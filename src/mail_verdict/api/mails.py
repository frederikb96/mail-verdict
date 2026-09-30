"""
Message API endpoints.

GET /api/accounts/:account_id/messages — cursor-paginated list, optionally
  grouped into one row per conversation (threaded=true)
GET /api/messages/:id — detail view (sanitized HTML, embedded verdict)
GET /api/messages/:id/thread — every message in the conversation, ascending
GET /api/messages/:id/attachments/:attachment_id — streamed attachment
GET /api/messages/:id/raw — the message's RFC822 source as a .eml download
POST /api/messages/:id/action — single-message action
POST /api/accounts/:account_id/messages/bulk-action — action over many
  messages, by id list or by a server-resolved scope

PostIMAP integration: SQL UPDATEs are sufficient for all actions --
postimap/actions.py owns the write shapes; PostIMAP's own triggers
propagate them to IMAP, and postimap/listener.py fans them out to SSE.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import ColumnElement, all_, any_, case, desc, func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from mail_verdict.api.events import get_event_ring
from mail_verdict.api.image_exceptions import (
    ImageAllowlist,
    is_sender_image_allowed,
    load_image_allowlist,
)
from mail_verdict.api.schemas import (
    AttachmentSummary,
    BulkActionRequest,
    BulkActionResponse,
    BulkActionScope,
    BulkActionSource,
    MessageActionRequest,
    MessageActionResponse,
    MessageDetail,
    MessageListResponse,
    MessageLocation,
    MessageQuoteResponse,
    MessageSummary,
    SelectionSnapshotResponse,
    TagResponse,
    ThreadResponse,
    VerdictResponse,
)
from mail_verdict.config import get_config
from mail_verdict.core.content_disposition import content_disposition
from mail_verdict.core.cursor import after_cursor, before_cursor
from mail_verdict.core.image_sanitizer import restore_remote_images, strip_remote_images
from mail_verdict.core.outbound_sanitizer import sanitize_outbound_html
from mail_verdict.core.sanitizer import (
    rewrite_remote_images,
    sanitize_email_html,
)
from mail_verdict.core.snippet import build_snippet
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import (
    AccountPrefs,
    Attachment,
    Folder,
    GlacierAttachment,
    GlacierMessage,
    MailTag,
    Message,
    Verdict,
)
from mail_verdict.database.repository import (
    FolderRepository,
    RowMarks,
    list_attachments_for_mails,
    list_latest_verdicts_for_mails,
    list_row_marks,
    list_tags_for_mails,
)
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.glacier.restore import start_restore
from mail_verdict.glacier.rows import (
    glacier_as_message_select,
    glacier_folder_ids,
    glacier_ids_among,
    resolve_glacier_id,
    was_permanently_expunged,
)
from mail_verdict.mail_actions.submissions import request_fingerprint, run_once
from mail_verdict.postimap.actions import (
    expunge,
    expunge_bulk,
    move_message,
    move_message_bulk,
    move_messages_from,
    set_flags,
    set_flags_bulk,
    set_keywords,
)
from mail_verdict.settings.service import get_settings_service

# The one shared refusal wording for an action a glaciered message
# cannot support -- so web, iOS and the API never say this three
# different ways.
GLACIER_REFUSAL_REASON = "This message is no longer on the mail server -- it is in the glacier."

logger = logging.getLogger(__name__)

# Detail/thread/action/attachment routes: /api/messages/...
router = APIRouter(prefix="/messages", tags=["messages"])

# List + bulk-action routes: /api/accounts/{account_id}/messages...
account_router = APIRouter(prefix="/accounts/{account_id}/messages", tags=["messages"])

# A list row (MessageSummary) never renders these -- deferring them keeps a
# page of the list from pulling the full raw message and its HTML body
# across the wire for every row just to read a sender and a subject.
# body_text stays eager: the list snippet (build_snippet) reads it, and a
# deferred attribute accessed outside the session's own greenlet raises
# MissingGreenlet rather than lazy-loading, the way it would sync.
_LIST_DEFERRED_COLUMNS = (
    defer(Message.raw_source),
    defer(Message.raw_headers),
    defer(Message.body_html),
)

# MessageDetail renders body_text/body_html but never raw_source (the
# entire RFC822 bytea, its own GET .../raw endpoint) or raw_headers (used
# only by the pipeline's MessageView, never by this response). Without
# this, get_message and get_thread pull the full raw message for every
# row just to produce about a kilobyte of JSON -- the thread endpoint
# doing it once per message in the conversation.
_DETAIL_DEFERRED_COLUMNS = (
    defer(Message.raw_source),
    defer(Message.raw_headers),
)


def _flat_summary(
    m: Message, marks: dict[uuid.UUID, RowMarks], glacier_ids: frozenset[uuid.UUID] = frozenset(),
) -> MessageSummary:
    """`glacier_ids` is which of this page's own ids are actually a
    glacier row -- needed because a union page's rows are all plain
    `Message` instances regardless of which table they came from
    (aliased(Message, union_subquery) returns Message objects for both
    arms), so imap_uid alone cannot tell a glacier row (always NULL,
    §2.2) from a live one with a move pending (also NULL). The
    glacier-only entity path could infer this from isinstance instead,
    but resolving it from ids here covers both paths with one rule."""
    is_glacier = m.id in glacier_ids
    return MessageSummary(
        has_attachments=marks[m.id].has_attachments,
        verdict_is_spam=marks[m.id].verdict_is_spam,
        id=m.id,
        account_id=m.account_id,
        folder_id=m.folder_id,
        thread_id=m.thread_id,
        subject=m.subject,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        received_at=m.received_at,
        is_seen=m.is_seen,
        is_flagged=m.is_flagged,
        is_answered=m.is_answered,
        is_draft=m.is_draft,
        is_truncated=m.is_truncated,
        pending_sync=False if is_glacier else m.imap_uid is None,
        snippet=build_snippet(m.body_text),
        mirrored_at=m.created_at,
        is_glacier=is_glacier,
    )


def _threaded_summary(
    m: Message, thread_count: int, unread_in_thread: int, marks: dict[uuid.UUID, RowMarks],
    glacier_ids: frozenset[uuid.UUID] = frozenset(),
) -> MessageSummary:
    is_glacier = m.id in glacier_ids
    return MessageSummary(
        has_attachments=marks[m.id].has_attachments,
        verdict_is_spam=marks[m.id].verdict_is_spam,
        id=m.id,
        account_id=m.account_id,
        folder_id=m.folder_id,
        thread_id=m.thread_id,
        subject=m.subject,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        received_at=m.received_at,
        is_seen=m.is_seen,
        is_flagged=m.is_flagged,
        is_answered=m.is_answered,
        is_draft=m.is_draft,
        is_truncated=m.is_truncated,
        pending_sync=False if is_glacier else m.imap_uid is None,
        snippet=build_snippet(m.body_text),
        thread_count=thread_count,
        unread_in_thread=unread_in_thread,
        mirrored_at=m.created_at,
        is_glacier=is_glacier,
    )


async def _resolve_glacier_ids(
    session: AsyncSession, entity: Any, ids: list[uuid.UUID],
) -> frozenset[uuid.UUID]:
    """Which of these ids are glacier rows, for _flat_summary/
    _threaded_summary's own is_glacier/pending_sync computation. Zero
    query cost on the untouched path (entity is Message): a real-folder
    list can never have one. The glacier-only path (entity is
    GlacierMessage) needs no query either -- every id already is one."""
    if entity is Message:
        return frozenset()
    if entity is GlacierMessage:
        return frozenset(ids)
    return await glacier_ids_among(session, ids)


@account_router.get("", response_model=MessageListResponse)
async def list_messages(
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None = Query(default=None),
    threaded: bool = Query(default=False, description="One row per conversation"),
    is_seen: bool | None = Query(default=None),
    since: datetime | None = Query(default=None),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last message in previous page -- fetches older",
    ),
    after: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of first message in previous page -- fetches newer",
    ),
    around: uuid.UUID | None = Query(
        default=None,
        description=(
            "Centre a fresh page on this message instead of starting at the newest "
            "edge -- half newer, half older. In threaded mode the target is resolved "
            "to its thread's own representative row first. Mutually exclusive with "
            "before/after."
        ),
    ),
    limit: int = Query(
        default=50, ge=1, le=1000,
        description=(
            "Rows per page. The ceiling is high enough for a client to re-read "
            "everything it has loaded in one request rather than page by page."
        ),
    ),
) -> MessageListResponse:
    """
    List messages with cursor-based pagination.

    threaded=true groups by thread_id: one row per conversation (its latest
    message, plus thread_count/unread_in_thread scoped to this same folder
    filter), ordered by that latest message's received_at.

    Three ways to page. The plain call starts at the newest edge. `before`
    continues older from a previous page's last row, exactly as it always
    has. `after` continues newer from a previous page's first row -- only
    meaningful once a page was ever centred away from the edge, since an
    ordinary page already starts there and has nothing newer to fetch.
    `around` centres a *fresh* page on a given message instead of the
    newest edge -- half newer, half older -- resolving it to its thread's
    own representative row first in threaded mode (the latest message in
    its thread among those matching this list's own filters), since that
    is the row the list actually renders; centring on the message itself
    would return a window the list never shows. 404 if the message
    doesn't exist, isn't in this account, or -- threaded -- its thread has
    no member matching folder_id/is_seen/since here: "not a member of
    this list" is a distinct answer from an ordinary empty page.
    """
    db = get_db_connection()
    async with db.session() as session:
        return await list_message_page(
            session, account_id=account_id, folder_id=folder_id, folder_scope=None,
            threaded=threaded, is_seen=is_seen, since=since,
            before=before, after=after, around=around, limit=limit,
        )


async def list_message_page(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    folder_scope: Any | None,
    threaded: bool,
    is_seen: bool | None,
    since: datetime | None,
    before: uuid.UUID | None,
    after: uuid.UUID | None,
    around: uuid.UUID | None,
    limit: int,
) -> MessageListResponse:
    """One page of a message list -- an account's own (account_id, and
    optionally folder_id) or a unified view's (folder_scope, see
    _list_filters). list_messages's docstring describes the paging and
    threading; a unified view's list pages and threads the same way.

    `as_of` is taken after the rows are read, from the clock rather than
    the transaction start (`now()`), so it is never earlier than the
    mirror time of anything the page could see."""
    page = await _read_message_page(
        session, account_id=account_id, folder_id=folder_id, folder_scope=folder_scope,
        threaded=threaded, is_seen=is_seen, since=since, before=before, after=after,
        around=around, limit=limit,
    )
    page.as_of = await session.scalar(select(func.clock_timestamp()))
    return page


async def _read_message_page(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    folder_scope: Any | None,
    threaded: bool,
    is_seen: bool | None,
    since: datetime | None,
    before: uuid.UUID | None,
    after: uuid.UUID | None,
    around: uuid.UUID | None,
    limit: int,
) -> MessageListResponse:
    """list_message_page without its as_of."""
    if around is not None and (before is not None or after is not None):
        raise HTTPException(
            status_code=400, detail="around is mutually exclusive with before/after",
        )

    entity = await _resolve_list_entity(
        session, account_id=account_id, folder_id=folder_id, folder_scope=folder_scope,
    )

    if around is not None:
        return await _list_messages_around(
            session, account_id, folder_id, threaded, is_seen, since, around, limit,
            folder_scope=folder_scope, entity=entity,
        )

    direction: Literal["older", "newer"] = "newer" if after is not None else "older"
    cursor_param = after if after is not None else before
    cursor_received_at, cursor_id = None, None
    if cursor_param is not None:
        cursor_result = await session.execute(
            select(entity.received_at, entity.id).where(entity.id == cursor_param)
        )
        cursor_row = cursor_result.one_or_none()
        if cursor_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cursor: message {cursor_param} not found",
            )
        cursor_received_at, cursor_id = cursor_row

    if threaded:
        rows = await _list_messages_threaded(
            session, account_id, folder_id, is_seen, since,
            cursor_received_at, cursor_id, limit, direction=direction,
            folder_scope=folder_scope, entity=entity,
        )
        overflow = len(rows) > limit
        page = rows[:limit]
        if direction == "newer":
            page = list(reversed(page))
        marks = await list_row_marks(session, [m.id for m, _tc, _uc in page])
        glacier_ids = await _resolve_glacier_ids(session, entity, [m.id for m, _tc, _uc in page])
        messages = [_threaded_summary(m, tc, uc, marks, glacier_ids) for m, tc, uc in page]
    else:
        all_msgs = await _list_messages_flat_page(
            session, account_id, folder_id, is_seen, since,
            cursor_received_at, cursor_id, limit, direction=direction,
            folder_scope=folder_scope, entity=entity,
        )
        overflow = len(all_msgs) > limit
        page_msgs = all_msgs[:limit]
        if direction == "newer":
            page_msgs = list(reversed(page_msgs))
        marks = await list_row_marks(session, [m.id for m in page_msgs])
        glacier_ids = await _resolve_glacier_ids(session, entity, [m.id for m in page_msgs])
        messages = [_flat_summary(m, marks, glacier_ids) for m in page_msgs]

    # Only the direction actually explored by this fetch is a genuinely
    # open question; the other stays at its safe default (nothing more)
    # since an ordinary single-directional page never needs the server
    # to answer it -- see the has_more_newer/prev_cursor field docs.
    has_more = overflow if direction == "older" else False
    has_more_newer = overflow if direction == "newer" else False

    next_cursor = str(messages[-1].id) if has_more and messages else None
    prev_cursor = str(messages[0].id) if has_more_newer and messages else None
    return MessageListResponse(
        messages=messages, has_more=has_more, next_cursor=next_cursor,
        has_more_newer=has_more_newer, prev_cursor=prev_cursor,
    )


async def _resolve_list_entity(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    folder_scope: Any | None,
) -> Any:
    """What every list helper below actually queries: `Message` itself,
    untouched, for a request that cannot reach a glacier at all (design
    section 4.2's "an installation with the feature off pays nothing" --
    this is the one query that decides it, once per page rather than
    once per helper).

    `folder_id` naming a glacier folder queries `GlacierMessage` directly
    -- no union, the cheaper and more common case (opening the Glacier
    folder itself), and no aliasing either: `GlacierMessage` is a proper
    mapped class in its own right, so `entity.<field>` and `select(entity)`
    already work on it exactly as they do on `Message`, without needing
    to fake up an ORM correspondence the way the union path below does.
    `_list_filters` adds the `visible_at IS NOT NULL` guard (D8) whenever
    `entity is GlacierMessage`, since nothing else on this path goes
    through glacier_as_message_select() to pick it up automatically.

    Anything wider that can still reach a glacier -- a unified view's
    folder_scope (member_folder_ids already unions a glacier membership
    in, so any folder_scope might carry one), or an account-wide/
    instance-wide list with no folder_id at all -- gets a UNION ALL of
    both tables (never plain UNION: a live id and a glacier id can never
    collide, so there is nothing to deduplicate) wrapped in a subquery
    and aliased back onto Message (database/repository.py's
    _build_candidate_query already uses the same trick for its own
    to_addrs branch). Unlike the glacier-only case, this DOES need
    `aliased(Message, ...)`: `entity.<field>` has to work as one thing
    spanning both tables, which only a real entity backed by the
    combined subquery can give it -- SQLAlchemy can only correlate that
    aliasing when the subquery's own first branch is itself an ORM
    `select(Message)`, which the union's live arm always is; a bare
    `select(*columns)` over `GlacierMessage` alone (as glacier_as_message
    _select() builds) carries no such anchor and aliased() cannot
    correlate to it standalone (as this function's glacier-only branch
    demonstrates by not even trying).

    A message's own thread_id survives glaciering unchanged (design
    section 3.9's "reads stay a plain thread_id equality union"), which
    is what makes grouping by thread_id after this union still correct.
    """
    known = await glacier_folder_ids(session)
    if not known:
        return Message

    if folder_id is not None:
        if folder_id in known:
            return GlacierMessage
        return Message

    if folder_scope is None and account_id is not None and account_id not in known.values():
        return Message

    live = select(Message).where(Message.expunged_at.is_(None))
    glacier = glacier_as_message_select()
    if account_id is not None:
        live = live.where(Message.account_id == account_id)
        glacier = glacier.where(GlacierMessage.account_id == account_id)
    if folder_scope is not None:
        live = live.where(Message.folder_id.in_(folder_scope))
        glacier = glacier.where(GlacierMessage.folder_id.in_(folder_scope))
    return aliased(Message, live.union_all(glacier).subquery())


def _list_filters(
    entity: Any,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    folder_scope: Any | None,
) -> list[ColumnElement[bool]]:
    """The WHERE clause every list helper below shares. `entity` is
    whatever _resolve_list_entity returned for this page -- `Message`
    itself, or an entity already scoped to one glacier folder or to a
    live+glacier union, in which case re-applying account_id/folder_id/
    folder_scope here is a harmless no-op (they already hold, by
    construction) rather than a second filter doing real work.
    folder_scope is a selectable of folder ids -- a unified view's member
    folders, which can span accounts -- for a list covering more than one
    folder; None for an ordinary account/folder list."""
    filters: list[ColumnElement[bool]] = [entity.expunged_at.is_(None)]
    if entity is GlacierMessage:
        # D8: NULL means invisible to every listing -- the glacier-only
        # path's own guard, since it never goes through
        # glacier_as_message_select() (which bakes this in for the union
        # path below) to pick this up automatically.
        filters.append(entity.visible_at.is_not(None))
    if account_id is not None:
        filters.append(entity.account_id == account_id)
    if folder_id is not None:
        filters.append(entity.folder_id == folder_id)
    if folder_scope is not None:
        filters.append(entity.folder_id.in_(folder_scope))
    if is_seen is not None:
        filters.append(entity.is_seen == is_seen)
    if since is not None:
        filters.append(entity.received_at >= since)
    return filters


async def _list_messages_around(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    threaded: bool,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
    limit: int,
    *,
    folder_scope: Any | None = None,
    entity: Any = Message,
) -> MessageListResponse:
    """A page centred on `around` rather than the newest edge -- see
    list_messages's own docstring for the threaded resolution and the
    not-a-member answer."""
    if threaded:
        resolved = await _resolve_around_threaded(
            session, account_id, folder_id, is_seen, since, around,
            folder_scope=folder_scope, entity=entity,
        )
    else:
        flat_target = await _resolve_around_flat(
            session, account_id, folder_id, is_seen, since, around,
            folder_scope=folder_scope, entity=entity,
        )
        resolved = (flat_target, 0, 0) if flat_target is not None else None

    if resolved is None:
        raise HTTPException(
            status_code=404, detail=f"Message {around} is not a member of this list",
        )
    target, target_thread_count, target_unread_in_thread = resolved

    # What's left after the target's own row is split roughly evenly; an
    # odd remainder goes to the newer half, since catching up to a live
    # tail is the direction most likely to matter again soon.
    remaining = max(limit - 1, 0)
    half_older = remaining // 2
    half_newer = remaining - half_older

    if threaded:
        older_rows = await _list_messages_threaded(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_older, direction="older",
            folder_scope=folder_scope, entity=entity,
        )
        newer_rows = await _list_messages_threaded(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_newer, direction="newer",
            folder_scope=folder_scope, entity=entity,
        )
        has_more = len(older_rows) > half_older
        has_more_newer = len(newer_rows) > half_newer
        combined = [
            *reversed(newer_rows[:half_newer]),
            (target, target_thread_count, target_unread_in_thread),
            *older_rows[:half_older],
        ]
        marks = await list_row_marks(session, [m.id for m, _tc, _uc in combined])
        glacier_ids = await _resolve_glacier_ids(
            session, entity, [m.id for m, _tc, _uc in combined],
        )
        messages = [_threaded_summary(m, tc, uc, marks, glacier_ids) for m, tc, uc in combined]
    else:
        older_msgs = await _list_messages_flat_page(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_older, direction="older",
            folder_scope=folder_scope, entity=entity,
        )
        newer_msgs = await _list_messages_flat_page(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_newer, direction="newer",
            folder_scope=folder_scope, entity=entity,
        )
        has_more = len(older_msgs) > half_older
        has_more_newer = len(newer_msgs) > half_newer
        combined_msgs = [*reversed(newer_msgs[:half_newer]), target, *older_msgs[:half_older]]
        marks = await list_row_marks(session, [m.id for m in combined_msgs])
        glacier_ids = await _resolve_glacier_ids(session, entity, [m.id for m in combined_msgs])
        messages = [_flat_summary(m, marks, glacier_ids) for m in combined_msgs]

    next_cursor = str(messages[-1].id) if has_more and messages else None
    prev_cursor = str(messages[0].id) if has_more_newer and messages else None
    return MessageListResponse(
        messages=messages, has_more=has_more, next_cursor=next_cursor,
        has_more_newer=has_more_newer, prev_cursor=prev_cursor,
    )


async def _resolve_around_flat(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
    *,
    folder_scope: Any | None = None,
    entity: Any = Message,
) -> Message | None:
    """The `around` target itself, if it matches this list's own filters --
    None otherwise (it doesn't exist, isn't in this account, or is filtered
    out), which the caller reports as "not a member of this list"."""
    stmt = select(entity).where(
        entity.id == around,
        *_list_filters(entity, account_id, folder_id, is_seen, since, folder_scope),
    )
    if entity is Message:
        stmt = stmt.options(*_LIST_DEFERRED_COLUMNS)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _resolve_around_threaded(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
    *,
    folder_scope: Any | None = None,
    entity: Any = Message,
) -> tuple[Message, int, int] | None:
    """Resolve `around` to the row that actually represents it in threaded
    mode: the latest message in its own thread among those matching this
    list's filters -- never `around` itself, which the list may not even
    show (its thread's newest row, possibly a different message, is what
    a threaded list renders). None means the thread has no member matching
    the filters at all -- a thread existing is not enough, since every one
    of its messages could still be filtered out (e.g. an unread-only view
    where this thread is fully read)."""
    thread_result = await session.execute(
        select(entity.thread_id).where(
            entity.id == around,
            *_list_filters(entity, account_id, None, None, None, None),
        )
    )
    thread_id = thread_result.scalar_one_or_none()
    if thread_id is None:
        return None

    filters = [
        *_list_filters(entity, account_id, folder_id, is_seen, since, folder_scope),
        entity.thread_id == thread_id,
    ]

    representative_stmt = (
        select(entity)
        .where(*filters)
        .order_by(desc(entity.received_at), desc(entity.id))
        .limit(1)
    )
    if entity is Message:
        representative_stmt = representative_stmt.options(*_LIST_DEFERRED_COLUMNS)
    representative_result = await session.execute(representative_stmt)
    representative = representative_result.scalar_one_or_none()
    if representative is None:
        return None

    stats_result = await session.execute(
        select(
            func.count(entity.id),
            func.count(case((entity.is_seen.is_(False), entity.id))),
        ).where(*filters)
    )
    thread_count, unread_in_thread = stats_result.one()
    return representative, thread_count, unread_in_thread


async def _list_messages_flat_page(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID | None,
    limit: int,
    *,
    direction: Literal["older", "newer"] = "older",
    folder_scope: Any | None = None,
    entity: Any = Message,
) -> list[Message]:
    """
    One page of ordinary (non-threaded) messages.

    "older" (the default, and the only direction used before `around`
    existed) is the ordinary continue-scrolling-down case, fetched newest
    first. "newer" is its mirror, used to grow a window that opened away
    from the newest edge back up toward it -- fetched oldest-of-the-newer
    first (closest to the cursor), since a keyset predicate can only walk
    forward from its cursor; the caller reverses it before rendering, since
    the list itself is always newest-first regardless of which direction a
    given page happened to be fetched in.

    `entity` is _resolve_list_entity's own choice for this page -- plain
    `Message` deferring raw_source/raw_headers as always, or an aliased
    glacier-only/union entity, which is queried whole (no defer options:
    they name Message's own columns, meaningless against a derived
    entity, and a glacier-involved page is the less common case this
    costs a little extra I/O on rather than the ordinary one).
    """
    stmt = select(entity).where(
        *_list_filters(entity, account_id, folder_id, is_seen, since, folder_scope)
    )
    if entity is Message:
        stmt = stmt.options(*_LIST_DEFERRED_COLUMNS)

    if direction == "older":
        stmt = stmt.order_by(desc(entity.received_at), desc(entity.id))
        if cursor_id is not None:
            stmt = stmt.where(
                after_cursor(entity.received_at, entity.id, cursor_received_at, cursor_id)
            )
    else:
        stmt = stmt.order_by(entity.received_at.asc(), entity.id.asc())
        if cursor_id is not None:
            stmt = stmt.where(
                before_cursor(entity.received_at, entity.id, cursor_received_at, cursor_id)
            )
    stmt = stmt.limit(limit + 1)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _list_messages_threaded(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID | None,
    limit: int,
    *,
    direction: Literal["older", "newer"] = "older",
    folder_scope: Any | None = None,
    entity: Any = Message,
) -> list[tuple[Message, int, int]]:
    """
    One row per thread_id: the latest message plus its thread's counts.

    Postgres DISTINCT ON picks the latest message per thread_id (ties broken
    by id); that result is joined against a per-thread aggregate (same
    filters) for thread_count/unread_in_thread, then re-ordered by
    received_at for cursor pagination -- DISTINCT ON's own ORDER BY must
    start with thread_id, so the "latest thread first" order has to be
    applied as an outer step, not folded into the same ORDER BY.

    Both the DISTINCT ON pick and the count aggregate are scoped to the
    same folder filter as the rest of the list: a thread's count here means
    "messages in this thread within this folder", matching the per-folder
    browsing the list itself is scoped to. When `entity` is a live+glacier
    union, this groups across both tables by thread_id equality, exactly
    as design section 3.9 says a read must ("reads stay a plain thread_id
    equality union") -- a message's own thread_id is unchanged by
    glaciering, so a conversation split across both tables still groups
    as one thread here.

    direction: see _list_messages_flat_page's own docstring -- the same
    "older" default / "newer" mirror, and the same reversal obligation on
    the caller.

    Both subqueries below scan every message the filter matches, not only
    the page eventually returned -- unlike the flat page's own query,
    which a LIMIT-friendly index lets Postgres satisfy without visiting
    rows outside the page at all. So this selects only the columns the
    pick and the join actually need (id, thread_id, received_at) rather
    than the full row, and fetches the handful of actual Message rows the
    page resolves to -- deferred the same way the flat page already is --
    in a second, id-scoped query once the page is known. A version of this
    selecting the full row up front carried body_html/raw_source/
    raw_headers through the sort for every candidate message in the
    folder, not just the ones returned.
    """
    filters = _list_filters(entity, account_id, folder_id, is_seen, since, folder_scope)

    latest_per_thread = (
        select(entity.id, entity.thread_id, entity.received_at)
        .where(*filters)
        .distinct(entity.thread_id)
        .order_by(entity.thread_id, desc(entity.received_at), desc(entity.id))
        .subquery("latest_per_thread")
    )

    thread_stats = (
        select(
            entity.thread_id.label("thread_id"),
            func.count(entity.id).label("thread_count"),
            func.count(case((entity.is_seen.is_(False), entity.id))).label("unread_in_thread"),
        )
        .where(*filters)
        .group_by(entity.thread_id)
        .subquery("thread_stats")
    )

    stmt = (
        select(
            latest_per_thread.c.id,
            thread_stats.c.thread_count,
            thread_stats.c.unread_in_thread,
        )
        .join(thread_stats, thread_stats.c.thread_id == latest_per_thread.c.thread_id)
    )
    received_at_col, id_col = latest_per_thread.c.received_at, latest_per_thread.c.id
    if direction == "older":
        stmt = stmt.order_by(desc(received_at_col), desc(id_col))
        if cursor_id is not None:
            stmt = stmt.where(after_cursor(received_at_col, id_col, cursor_received_at, cursor_id))
    else:
        stmt = stmt.order_by(received_at_col.asc(), id_col.asc())
        if cursor_id is not None:
            stmt = stmt.where(before_cursor(received_at_col, id_col, cursor_received_at, cursor_id))
    stmt = stmt.limit(limit + 1)

    page = (await session.execute(stmt)).all()
    if not page:
        return []

    ids_stmt = select(entity).where(entity.id.in_([row.id for row in page]))
    if entity is Message:
        ids_stmt = ids_stmt.options(*_LIST_DEFERRED_COLUMNS)
    messages_by_id = {m.id: m for m in (await session.execute(ids_stmt)).scalars()}
    return [(messages_by_id[row.id], row.thread_count, row.unread_in_thread) for row in page]


@router.get("/{message_id}/location", response_model=MessageLocation)
async def locate_message(message_id: uuid.UUID) -> MessageLocation:
    """
    Where a message is now -- account, folder and thread, without the body.

    A message moved through this API keeps its id, so that is simply its
    own row. One moved in another mail client is a different row: PostIMAP
    mirrors that as an expunge in the source folder plus an insert in the
    destination, the pair sharing only the Message-ID header (see the
    consumer contract). So an expunged row resolves to the live row
    carrying the same header on the same account, when there is one. 404
    when there is not -- the message is gone, not merely elsewhere.
    """
    db = get_db_connection()
    async with db.session() as session:
        row = (await session.execute(
            select(
                Message.id, Message.account_id, Message.folder_id, Message.thread_id,
                Message.expunged_at, Message.message_id,
            ).where(Message.id == message_id)
        )).one_or_none()
        if row is None:
            glacier_row = (await session.execute(
                select(
                    GlacierMessage.id, GlacierMessage.account_id, GlacierMessage.folder_id,
                    GlacierMessage.thread_id, GlacierMessage.restored_at,
                    GlacierMessage.msg_key,
                ).where(GlacierMessage.id == message_id)
            )).one_or_none()
            if glacier_row is None:
                raise HTTPException(status_code=404, detail="Message not found")
            if glacier_row.restored_at is None:
                return MessageLocation(
                    id=glacier_row.id, account_id=glacier_row.account_id,
                    folder_id=glacier_row.folder_id, thread_id=glacier_row.thread_id,
                )
            # A tombstone (restored earlier): resolve to whatever live row
            # its restore produced. msg_key IS the Message-ID header,
            # angle brackets included the same way messages.message_id
            # stores it, whenever it isn't the hash-fallback form for a
            # message with no header at all -- which never matches a
            # restored row either, so there is nothing to resolve to.
            live_twin = None
            if not glacier_row.msg_key.startswith("sha256:"):
                live_twin = (await session.execute(
                    select(Message.id, Message.account_id, Message.folder_id, Message.thread_id)
                    .where(
                        Message.account_id == glacier_row.account_id,
                        Message.message_id == glacier_row.msg_key,
                        Message.expunged_at.is_(None),
                    )
                    .order_by(desc(Message.created_at))
                    .limit(1)
                )).one_or_none()
            if live_twin is None:
                raise HTTPException(status_code=404, detail="Message no longer exists")
            return MessageLocation(
                id=live_twin.id, account_id=live_twin.account_id,
                folder_id=live_twin.folder_id, thread_id=live_twin.thread_id,
            )
        if row.expunged_at is None:
            return MessageLocation(
                id=row.id, account_id=row.account_id,
                folder_id=row.folder_id, thread_id=row.thread_id,
            )
        if row.message_id is None:
            raise HTTPException(status_code=404, detail="Message no longer exists")

        twin = (await session.execute(
            select(Message.id, Message.account_id, Message.folder_id, Message.thread_id)
            .where(
                Message.account_id == row.account_id,
                Message.message_id == row.message_id,
                Message.expunged_at.is_(None),
            )
            .order_by(Message.imap_uid.is_(None), desc(Message.created_at))
            .limit(1)
        )).one_or_none()
        if twin is None:
            # No live twin either -- the expunge could be this message
            # having been glaciered rather than moved by another client,
            # which leaves origin_message_id pointing at exactly this id.
            glacier_id = await resolve_glacier_id(session, message_id)
            if glacier_id is not None:
                glacier_twin = (await session.execute(
                    select(
                        GlacierMessage.id, GlacierMessage.account_id,
                        GlacierMessage.folder_id, GlacierMessage.thread_id,
                    ).where(GlacierMessage.id == glacier_id)
                )).one_or_none()
                if glacier_twin is not None:
                    return MessageLocation(
                        id=glacier_twin.id, account_id=glacier_twin.account_id,
                        folder_id=glacier_twin.folder_id, thread_id=glacier_twin.thread_id,
                    )
            raise HTTPException(status_code=404, detail="Message no longer exists")
    return MessageLocation(
        id=twin.id, account_id=twin.account_id,
        folder_id=twin.folder_id, thread_id=twin.thread_id,
    )


def _to_message_detail(
    m: Message | GlacierMessage,
    *,
    body_html: str | None,
    has_blocked_images: bool,
    images_allowed: bool,
    tags: list[MailTag],
    attachments: list[Attachment] | list[Any],
    verdict: Verdict | None,
    is_glacier: bool = False,
    origin_folder_name: str | None = None,
) -> MessageDetail:
    """
    Assemble a MessageDetail from a message row plus its tags, attachments
    and latest verdict -- the one place get_message and get_thread agree
    on the response shape, so the fields either endpoint returns cannot
    silently drift apart from the other. Callers own sanitizing body_html
    and deciding has_blocked_images/images_allowed, since get_message and
    get_thread apply load_images differently -- see each one's docstring.

    `m` is a Message or, for a glaciered message, a GlacierMessage --
    every field read below has the same name on both, by design (see
    that model's own docstring), so this function needs no branch to
    tell them apart.
    """
    return MessageDetail(
        id=m.id,
        account_id=m.account_id,
        folder_id=m.folder_id,
        thread_id=m.thread_id,
        # A glacier row's imap_uid is always NULL -- unlike a live row,
        # that means "not on the server", not "move pending".
        pending_sync=False if is_glacier else m.imap_uid is None,
        is_truncated=m.is_truncated,
        message_id=m.message_id,
        subject=m.subject,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        cc_addrs=m.cc_addrs,
        bcc_addrs=m.bcc_addrs,
        reply_to=m.reply_to,
        in_reply_to=m.in_reply_to,
        references=m.msg_references,
        body_text=m.body_text,
        body_html=body_html,
        received_at=m.received_at,
        size_bytes=m.size_bytes,
        is_seen=m.is_seen,
        is_flagged=m.is_flagged,
        is_answered=m.is_answered,
        is_draft=m.is_draft,
        keywords=m.keywords or [],
        snippet=build_snippet(m.body_text),
        created_at=m.created_at,
        has_blocked_images=has_blocked_images,
        images_allowed=images_allowed,
        is_glacier=is_glacier,
        origin_folder_name=origin_folder_name,
        tags=[TagResponse(tag_name=t.tag_name, source=t.source.value) for t in tags],
        attachments=[
            AttachmentSummary(
                id=a.id, filename=a.filename, content_type=a.content_type, size_bytes=a.size_bytes,
            )
            for a in attachments
        ],
        verdict=(
            VerdictResponse(
                id=verdict.id, message_id=verdict.mail_id, is_spam=verdict.is_spam,
                model_used=verdict.model_used, reasoning=verdict.reasoning,
                source=verdict.source.value, created_at=verdict.created_at,
            )
            if verdict
            else None
        ),
    )


@router.get("/{message_id}", response_model=MessageDetail)
async def get_message(
    message_id: uuid.UUID,
    load_images: bool = Query(default=False, description="Load remote images if allowed"),
) -> MessageDetail:
    """
    Get full message detail by ID.

    Returns the message with attachments, tags, the current verdict (if
    any), and image privacy controls. HTML is sanitized here at read time
    -- PostIMAP owns the insert, so body_html is untrusted until this pass.

    load_images defaults to false, so a caller passing true has already
    made a deliberate choice -- "Load for this message" restores images
    for that one response whether or not the sender is allowlisted.
    images_allowed in the response still reports the sender's own,
    unchanged allowlist status; only body_html and has_blocked_images
    reflect the override.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Message)
            .options(*_DETAIL_DEFERRED_COLUMNS)
            .where(Message.id == message_id, Message.expunged_at.is_(None))
        )
        msg: Message | GlacierMessage | None = result.scalar_one_or_none()
        is_glacier = False
        origin_folder_name = None
        resolved_id = message_id
        if msg is None:
            # message_id may be a glacier row's own id, or the *original*
            # live id a message had before it was glaciered -- a client
            # that had it open, a saved link, a draft reply already
            # pointing at it. Either way resolved_id becomes the glacier
            # row's own id from here on: mail_tags/verdicts are repointed
            # to it at glacier time (design section 2.5), so looking them
            # up under the original id would silently find nothing.
            glacier_id = await resolve_glacier_id(session, message_id)
            if glacier_id is not None:
                glacier_result = await session.execute(
                    select(GlacierMessage).where(GlacierMessage.id == glacier_id)
                )
                msg = glacier_result.scalar_one_or_none()
            if msg is not None:
                is_glacier = True
                origin_folder_name = msg.origin_imap_name
                resolved_id = msg.id
            elif await was_permanently_expunged(session, message_id):
                # Destroyed on purpose through the glacier's own delete-
                # forever action -- never the "readable stale copy"
                # fallback below, which exists for an ordinary client-
                # side move, not for something deleted deliberately.
                raise HTTPException(status_code=404, detail="Message not found")
            else:
                # Not glaciered -- an ordinary expunge (moved by another
                # mail client). mail_tags/verdicts/attachments are never
                # repointed for that case, so the expunged row's own id
                # still keys them correctly; fall back to it rather than
                # 404ing on a message that still has a readable copy.
                stale_result = await session.execute(
                    select(Message)
                    .options(*_DETAIL_DEFERRED_COLUMNS)
                    .where(Message.id == message_id)
                )
                msg = stale_result.scalar_one_or_none()
                if msg is None:
                    raise HTTPException(status_code=404, detail="Message not found")

        tags = (await list_tags_for_mails(session, [resolved_id]))[resolved_id]
        attachments: list[Any]
        if is_glacier:
            attachments = list(
                (
                    await session.execute(
                        select(GlacierAttachment).where(
                            GlacierAttachment.glacier_message_id == resolved_id,
                        )
                    )
                ).scalars().all()
            )
        else:
            attachments = (await list_attachments_for_mails(session, [resolved_id]))[resolved_id]
        verdict = (await list_latest_verdicts_for_mails(session, [resolved_id])).get(resolved_id)

        body_html = msg.body_html
        images_allowed = False
        has_blocked_images = False
        if body_html:
            body_html = sanitize_email_html(body_html)
            body_html = _rewrite_cid_references(body_html, resolved_id, attachments)

            allowlist = await load_image_allowlist(session, msg.account_id)
            images_allowed = allowlist.allows(msg.from_addr)

            # load_images defaults to false here, unlike get_thread's own
            # default of true (see its own docstring) -- so a caller of this
            # endpoint passing true has always made a deliberate, one-off ask,
            # whether or not the sender happens to be allowlisted. Gating it
            # on images_allowed too, the way get_thread's own default call
            # must, would make the override a no-op for exactly the senders
            # it exists to help with.
            if load_images:
                body_html = restore_remote_images(body_html)
                has_blocked_images = False
            else:
                body_html, has_blocked_images = strip_remote_images(body_html)

        return _to_message_detail(
            msg,
            body_html=body_html,
            has_blocked_images=has_blocked_images,
            images_allowed=images_allowed,
            tags=tags,
            attachments=attachments,
            verdict=verdict,
            is_glacier=is_glacier,
            origin_folder_name=origin_folder_name,
        )


def _rewrite_cid_references(
    body_html: str, message_id: uuid.UUID, attachments: list[Any],
) -> str:
    """
    Rewrite cid: image sources to the attachment streaming endpoint.

    Inline images referenced by Content-ID resolve through
    GET /messages/{id}/attachments/{attachment_id} rather than staying as
    a cid: URI the browser cannot fetch directly -- id-agnostic (design
    section 4.2), so a glaciered message's own attachments need no
    branch here: only content_id and id are read, and GlacierAttachment
    carries both under the same names.
    """
    import re

    by_content_id = {a.content_id.strip("<>"): a.id for a in attachments if a.content_id}
    if not by_content_id:
        return body_html

    def _replace(match: re.Match[str]) -> str:
        cid = match.group(2)
        attachment_id = by_content_id.get(cid)
        if attachment_id is None:
            return match.group(0)
        return f'{match.group(1)}/api/messages/{message_id}/attachments/{attachment_id}"'

    return re.sub(r'(\bsrc\s*=\s*")cid:([^"]+)"', _replace, body_html, flags=re.IGNORECASE)


@router.get("/{message_id}/thread", response_model=ThreadResponse)
async def get_thread(
    message_id: uuid.UUID,
    load_images: bool = Query(default=True, description="Load remote images if allowed"),
) -> ThreadResponse:
    """
    Get every message in this message's conversation, across folders, ascending.

    This is how a Sent reply appears inside the thread it belongs to --
    thread_id groups across folders, not just within the one the anchor
    message happens to be in.

    load_images defaults to true here, unlike get_message's own default
    of false: the reading pane calls this endpoint with no query string
    at all and has always shown an allowlisted sender's images the
    moment the thread opens, with no separate "load images" click --
    changing the default would be a real, user-visible regression for
    zero benefit, since is_sender_image_allowed is what actually decides
    trust. The parameter exists so a caller that does want the pre-image
    state (get_message's own use, or a future one) has a way to ask for
    it, the same shape both endpoints now share.
    """
    db = get_db_connection()
    async with db.session() as session:
        anchor = await session.execute(select(Message.thread_id).where(Message.id == message_id))
        thread_id = anchor.scalar_one_or_none()
        if thread_id is None:
            glacier_anchor = await session.execute(
                select(GlacierMessage.thread_id).where(
                    GlacierMessage.id == message_id, GlacierMessage.visible_at.is_not(None),
                )
            )
            thread_id = glacier_anchor.scalar_one_or_none()
        if thread_id is None:
            raise HTTPException(status_code=404, detail="Message not found")

        # A conversation is never entirely one table once any of its
        # messages has been glaciered -- a live message's own thread_id
        # is unchanged by copy_message (design section 3), so the
        # glaciered half and whatever stayed live both carry it. Two
        # queries, one per table, unioned in Python and re-sorted by
        # received_at -- the "column-compatible union" pattern
        # (glacier/rows.py) done directly here rather than through a SQL
        # UNION, since both halves already need their own full ORM rows
        # for tags/attachments/verdict lookups below regardless.
        live_result = await session.execute(
            select(Message)
            .options(*_DETAIL_DEFERRED_COLUMNS)
            .where(Message.thread_id == thread_id, Message.expunged_at.is_(None))
        )
        live_messages: list[Message | GlacierMessage] = list(live_result.scalars().all())
        glacier_result = await session.execute(
            select(GlacierMessage).where(
                GlacierMessage.thread_id == thread_id, GlacierMessage.visible_at.is_not(None),
            )
        )
        glacier_messages = list(glacier_result.scalars().all())
        glacier_ids = {m.id for m in glacier_messages}

        _min_dt = datetime.min.replace(tzinfo=timezone.utc)
        thread_messages = sorted(
            [*live_messages, *glacier_messages], key=lambda m: m.received_at or _min_dt,
        )
        mail_ids = [m.id for m in thread_messages]

        # Four queries total for the whole conversation rather than four
        # per message: batching tags/attachments/verdicts here is what
        # keeps get_thread's own cost from scaling with thread length.
        tags_by_mail = await list_tags_for_mails(session, mail_ids)
        attachments_by_mail = await list_attachments_for_mails(session, mail_ids)
        verdicts_by_mail = await list_latest_verdicts_for_mails(session, mail_ids)

        # A thread never spans accounts (thread_id is resolved and indexed
        # per account -- see postimap/actions.py's own note on how a Sent
        # reply resolves onto it), so one allowlist load covers every
        # sender in the conversation.
        allowlist = (
            await load_image_allowlist(session, thread_messages[0].account_id)
            if thread_messages
            else ImageAllowlist(frozenset(), frozenset())
        )

        details: list[MessageDetail] = []
        for m in thread_messages:
            attachments = attachments_by_mail[m.id]
            verdict = verdicts_by_mail.get(m.id)
            is_glacier = m.id in glacier_ids

            body_html = m.body_html
            if body_html:
                body_html = sanitize_email_html(body_html)
                body_html = _rewrite_cid_references(body_html, m.id, attachments)
                images_allowed = allowlist.allows(m.from_addr)
                body_html, has_blocked = (
                    (restore_remote_images(body_html), False)
                    if images_allowed and load_images
                    else strip_remote_images(body_html)
                )
            else:
                images_allowed, has_blocked = False, False

            details.append(
                _to_message_detail(
                    m,
                    body_html=body_html,
                    has_blocked_images=has_blocked,
                    images_allowed=images_allowed,
                    tags=tags_by_mail[m.id],
                    attachments=attachments,
                    verdict=verdict,
                    is_glacier=is_glacier,
                    origin_folder_name=m.origin_imap_name if is_glacier else None,  # type: ignore[union-attr]
                )
            )
        return ThreadResponse(messages=details)


@router.get("/{message_id}/attachments/{attachment_id}")
async def get_attachment(message_id: uuid.UUID, attachment_id: uuid.UUID) -> Response:
    """Stream an attachment's bytes with its content type and a download disposition."""
    db = get_db_connection()
    async with db.session() as session:
        # Same reasoning as get_raw_source: the query below has no
        # expunged_at filter (attachments of an ordinary expunge stay
        # downloadable), so a message destroyed on purpose through the
        # glacier is checked for first.
        if await was_permanently_expunged(session, message_id):
            raise HTTPException(status_code=404, detail="Attachment not found")
        result = await session.execute(
            select(Attachment).where(
                Attachment.id == attachment_id, Attachment.message_id == message_id,
            )
        )
        attachment: Attachment | GlacierAttachment | None = result.scalar_one_or_none()
        if attachment is None:
            glacier_id = await resolve_glacier_id(session, message_id)
            if glacier_id is not None:
                glacier_result = await session.execute(
                    select(GlacierAttachment).where(
                        GlacierAttachment.id == attachment_id,
                        GlacierAttachment.glacier_message_id == glacier_id,
                    )
                )
                attachment = glacier_result.scalar_one_or_none()

    if attachment is None or attachment.data is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    content_type = attachment.content_type or "application/octet-stream"
    filename = attachment.filename or str(attachment_id)
    return Response(
        content=attachment.data,
        media_type=content_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/{message_id}/raw")
async def get_raw_source(message_id: uuid.UUID) -> Response:
    """
    Download a message's full RFC822 source as a .eml file.

    raw_source is the entire message on every row, so this is its own
    single-column query rather than reusing MessageDetail -- and NULL when
    is_truncated, since a message over storage.max_message_bytes was never
    fetched from IMAP at all.
    """
    db = get_db_connection()
    async with db.session() as session:
        # Checked before the stale-`messages`-row query below even runs:
        # that query has no expunged_at filter at all (an ordinary
        # client-side expunge deliberately keeps serving its readable
        # copy under the old id), which would otherwise serve this
        # message's raw bytes straight from the stale row regardless of
        # having been destroyed on purpose through the glacier.
        if await was_permanently_expunged(session, message_id):
            raise HTTPException(status_code=404, detail="Message not found")
        result = await session.execute(
            select(Message.subject, Message.raw_source, Message.is_truncated)
            .where(Message.id == message_id)
        )
        row = result.one_or_none()
        if row is None:
            glacier_id = await resolve_glacier_id(session, message_id)
            if glacier_id is not None:
                glacier_result = await session.execute(
                    select(
                        GlacierMessage.subject, GlacierMessage.raw_source,
                        GlacierMessage.is_truncated,
                    ).where(GlacierMessage.id == glacier_id)
                )
                row = glacier_result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row.raw_source is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No raw source stored for this message -- it exceeded "
                "storage.max_message_bytes when it was fetched and was "
                "never downloaded from IMAP."
                if row.is_truncated
                else "No raw source stored for this message."
            ),
        )

    filename = f"{row.subject or 'message'}.eml"
    return Response(
        content=row.raw_source,
        media_type="message/rfc822",
        headers={"Content-Disposition": content_disposition(filename)},
    )


def _text_to_html(text: str) -> str:
    """Escape plain text and join its lines with <br>, for quoting a
    message that never had an HTML part at all."""
    return "<br>".join(html.escape(line) for line in text.splitlines())


@router.get("/{message_id}/quote", response_model=MessageQuoteResponse)
async def get_message_quote(message_id: uuid.UUID) -> MessageQuoteResponse:
    """
    A message's body as HTML, shaped for local display, for embedding as a
    reply or forward quote in the compose editor.

    Reads the raw body_html column rather than the display-shaped one
    get_message returns: that copy has cid: images rewritten to local,
    unauthenticated attachment URLs, which means nothing to a message
    actually being sent. Starting from the raw column and running it
    through the same outbound sanitiser every other producer of
    outbox.body_html goes through keeps that mapping in one place -- a
    remote image quotes as the sender's own absolute URL, a cid: or
    locally-rewritten one simply disappears, since there is nothing to
    attach it to.

    That real-URL form is then rewritten to the same data-x-src/data-x-bg
    placeholder the reading pane uses, and restored only if this message's
    own sender is already allowlisted -- the read path's own rule, applied
    here too because the editor's quote node renders this HTML locally
    (assigns it to innerHTML), where an unrewritten remote image would
    fetch on every reply, reply-all, forward and reopened draft regardless
    of whether the sender has ever been allowed to. Whatever placeholder
    survives that is restored again, unconditionally, by create_outbox()
    before a message actually leaves -- an allowlist decision about what
    loads automatically in this reader is not a decision about what the
    person being forwarded to gets to see.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(
                Message.body_html, Message.body_text,
                Message.account_id, Message.from_addr,
            ).where(
                Message.id == message_id, Message.expunged_at.is_(None),
            )
        )
        row = result.one_or_none()
        if row is None:
            glacier_id = await resolve_glacier_id(session, message_id)
            if glacier_id is not None:
                glacier_result = await session.execute(
                    select(
                        GlacierMessage.body_html, GlacierMessage.body_text,
                        GlacierMessage.account_id, GlacierMessage.from_addr,
                    ).where(GlacierMessage.id == glacier_id)
                )
                row = glacier_result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")

    body_html, body_text, account_id, from_addr = row
    if body_html:
        # restore_remote_images must never run on the raw column: it
        # splices a data-x-style/data-x-stylesheet marker's stored value
        # back in as markup or raw <style> content, and a sender can write
        # one of those attribute names directly in the mail they send.
        # sanitize_email_html runs first so nh3 strips a sender-authored
        # copy before anything is restored, and rewrite_remote_images
        # (which sanitize_email_html already includes) re-derives real
        # markers from whatever remote references the message actually
        # has -- restoring those is what this endpoint needs, to quote a
        # remote image as the sender's own absolute URL rather than as
        # this reader's internal placeholder.
        sanitized = sanitize_outbound_html(restore_remote_images(sanitize_email_html(body_html)))
        display_html = rewrite_remote_images(sanitized)
        if await is_sender_image_allowed(account_id, from_addr):
            display_html = restore_remote_images(display_html)
        return MessageQuoteResponse(html=display_html)
    if body_text:
        return MessageQuoteResponse(html=f"<p>{_text_to_html(body_text)}</p>")
    return MessageQuoteResponse(html="")


@router.post("/{message_id}/action", response_model=MessageActionResponse)
async def message_action(
    message_id: uuid.UUID,
    request: MessageActionRequest,
) -> MessageActionResponse:
    """
    Perform an action on a message.

    Updates the local DB immediately. PostIMAP's PG trigger propagates
    changes to IMAP; postimap/listener.py fans the resulting event out
    to SSE, so this handler never emits one itself. A request carrying an
    idempotency_key is applied once however often it is repeated
    (mail_actions/submissions.py).
    """
    if request.idempotency_key is None:
        return await _apply_and_locate(message_id, request)
    return await run_once(
        get_db_connection(),
        request.idempotency_key,
        request_fingerprint("message", message_id, request),
        MessageActionResponse,
        lambda: _apply_and_locate(message_id, request),
    )


async def _apply_and_locate(
    message_id: uuid.UUID, request: MessageActionRequest,
) -> MessageActionResponse:
    """Apply the action, then say which folder the message is in now."""
    response = await _apply_message_action(message_id, request)
    if response.applied and response.folder_id is None:
        # A branch that already set folder_id itself (moving into the
        # glacier, whose id and folder no longer belong to `messages` at
        # all by the time this runs) knows better than this generic
        # re-read, which only ever looks at the live table under the
        # original id.
        async with get_db_connection().session() as session:
            response.folder_id = await session.scalar(
                select(Message.folder_id).where(Message.id == message_id)
            )
    return response


async def _apply_message_action(
    message_id: uuid.UUID, request: MessageActionRequest,
) -> MessageActionResponse:
    """Perform one message action -- message_action without its key.

    An expunged message is gone: acting on it would only reach a server
    that no longer holds it and come back as a write failure. With
    expected_folder_id, a message no longer in that folder is left alone
    and the response says applied=false.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        msg = result.scalar_one_or_none()
    if msg is None or msg.expunged_at is not None:
        glacier_response = await _apply_glacier_message_action(message_id, request)
        if glacier_response is not None:
            return glacier_response
        raise HTTPException(status_code=404, detail="Message not found")

    action = request.action
    account_id = msg.account_id
    expected = request.expected_folder_id
    if expected is not None and msg.folder_id != expected:
        # Already where the action files it -- the same request applied by
        # a run that died before answering, or the same filing made
        # elsewhere -- is the action done, not a guard miss.
        if msg.folder_id == await _action_target(account_id, action, request.target_folder_id):
            return MessageActionResponse(
                success=True, action=action, message_id=message_id, folder_id=msg.folder_id,
            )
        return _not_applied(action, message_id)

    if action == "move" and request.target_folder_id is not None:
        async with db.session() as session:
            glacier_account_id = await _glacier_account_for_folder(
                session, request.target_folder_id,
            )
        if glacier_account_id is not None:
            if glacier_account_id != account_id:
                raise HTTPException(
                    status_code=400,
                    detail="target_folder_id does not belong to this account",
                )
            outcome = await glacier_message_now(db, message_id, event_ring=get_event_ring())
            return MessageActionResponse(
                success=outcome.ok, action=action,
                message_id=outcome.glacier_id if outcome.glacier_id is not None else message_id,
                folder_id=request.target_folder_id if outcome.ok else None,
                message=outcome.reason, applied=outcome.ok,
            )

    if action in ("mark_read", "mark_unread", "flag", "unflag"):
        flag = {"mark_read": ("is_seen", True), "mark_unread": ("is_seen", False),
                "flag": ("is_flagged", True), "unflag": ("is_flagged", False)}[action]
        async with db.session() as session:
            if expected is None:
                await set_flags(session, message_id, **{flag[0]: flag[1]})
            else:
                await set_flags_bulk(
                    session, [message_id], expected_folder_id=expected, **{flag[0]: flag[1]},
                )
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action == "keyword_add" or action == "keyword_remove":
        if not request.keyword:
            raise HTTPException(status_code=400, detail=f"keyword required for {action}")
        current = set(msg.keywords or [])
        current = current | {request.keyword} if action == "keyword_add" else current - {
            request.keyword,
        }
        async with db.session() as session:
            await set_keywords(session, message_id, sorted(current))
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action == "expunge":
        async with db.session() as session:
            if expected is None:
                await expunge(session, message_id)
            elif not await expunge_bulk(session, [message_id], expected_folder_id=expected):
                return _not_applied(action, message_id)
        return MessageActionResponse(
            success=True, action=action, message_id=message_id, message="Permanently deleted",
        )

    if action in ("move", "archive", "trash"):
        if action == "move":
            if not request.target_folder_id:
                raise HTTPException(status_code=400, detail="target_folder_id required for move")
            target_folder_id: uuid.UUID = request.target_folder_id
            target_role = await FolderRepository(db).get_effective_special_use(target_folder_id)
        else:
            resolved_target = await _resolve_special_folder(account_id, action)
            if resolved_target is None:
                raise HTTPException(
                    status_code=400, detail=f"No {action} folder found for this account",
                )
            target_folder_id = resolved_target
            target_role = action
        async with db.session() as session:
            if action == "move" and not await _folder_belongs_to_account(
                session, account_id, target_folder_id,
            ):
                raise HTTPException(
                    status_code=400, detail="target_folder_id does not belong to this account",
                )
            if expected is None:
                await move_message(session, message_id, target_folder_id)
            elif expected != target_folder_id and not await move_messages_from(
                session, [message_id], expected, target_folder_id,
            ):
                return _not_applied(action, message_id)
            if _should_mark_read_on_file(target_role):
                await set_flags(session, message_id, is_seen=True)
        return MessageActionResponse(
            success=True, action=action, message_id=message_id,
            message="Moved to trash" if action == "trash" else None,
        )

    if action in ("spam", "not_spam"):
        return await _handle_spam_action(
            message_id, account_id, is_spam=action == "spam", expected_folder_id=expected,
        )

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


async def _apply_glacier_message_action(
    glacier_id: uuid.UUID, request: MessageActionRequest,
) -> MessageActionResponse | None:
    """
    The action table for a message already in the glacier (this
    feature's design, section 6). Read/star/keywords work directly
    against the glacier row -- MailVerdict owns those columns on it, the
    same as on a live message. move/archive/trash restore it to the
    server. expunge here is genuinely permanent: it is the only copy
    that exists, so it requires request.confirm. spam/not_spam are not
    yet supported against a glaciered message.

    Returns:
        None if glacier_id names neither a visible glacier row (its own
        id or the original pre-glacier id) nor a tombstone (the caller
        then reports the ordinary 404), otherwise a response -- possibly
        success=false, never a silent no-op
    """
    db = get_db_connection()
    action = request.action
    async with db.session() as session:
        resolved_id = await resolve_glacier_id(session, glacier_id)
        row = None
        if resolved_id is not None:
            row = (
                await session.execute(
                    select(GlacierMessage).where(GlacierMessage.id == resolved_id)
                )
            ).scalar_one_or_none()
        if row is None:
            # Not live under this id or its origin -- a tombstone (already
            # restored) gets a reason a client can act on, since silently
            # acting on the now-live restored message under a stale
            # reference is a write the caller never asked for.
            tombstone = await session.execute(
                select(GlacierMessage.id).where(
                    GlacierMessage.id == glacier_id, GlacierMessage.restored_at.is_not(None),
                )
            )
            if tombstone.scalar_one_or_none() is not None:
                return MessageActionResponse(
                    success=False, action=action, message_id=glacier_id, applied=False,
                    message="This message has already been restored to the mail server -- "
                    "look it up again to act on the restored copy.",
                )
            return None
    glacier_id = row.id
    # The glacier's own synthetic folder id -- honest for every one of
    # this function's responses below, since MailVerdict owns where a
    # glacier row lists and nothing here moves it out of the glacier
    # synchronously (a restore only actually leaves once confirm_restores
    # observes the server's copy). The generic re-read in message_action()
    # only ever looks at `messages`, which has nothing useful to say about
    # a glacier row or the stale expunged live row still sitting under its
    # origin id -- every branch below sets this explicitly so that re-read
    # is never reached.
    glacier_folder_id = row.folder_id

    if request.expected_folder_id is not None and row.folder_id != request.expected_folder_id:
        return _not_applied(action, glacier_id)

    if action in ("mark_read", "mark_unread", "flag", "unflag"):
        column, value = {
            "mark_read": ("is_seen", True), "mark_unread": ("is_seen", False),
            "flag": ("is_flagged", True), "unflag": ("is_flagged", False),
        }[action]
        async with db.session() as session:
            await session.execute(
                text(f"UPDATE glacier_messages SET {column} = :value WHERE id = :id"),  # noqa: S608
                {"value": value, "id": glacier_id},
            )
        await _announce_glacier_change(row.account_id, glacier_id, row.folder_id)
        return MessageActionResponse(
            success=True, action=action, message_id=glacier_id, folder_id=glacier_folder_id,
        )

    if action in ("keyword_add", "keyword_remove"):
        if not request.keyword:
            raise HTTPException(status_code=400, detail=f"keyword required for {action}")
        current = set(row.keywords or [])
        current = (
            current | {request.keyword} if action == "keyword_add"
            else current - {request.keyword}
        )
        async with db.session() as session:
            await session.execute(
                text("UPDATE glacier_messages SET keywords = :kw WHERE id = :id"),
                {"kw": sorted(current), "id": glacier_id},
            )
        await _announce_glacier_change(row.account_id, glacier_id, row.folder_id)
        return MessageActionResponse(
            success=True, action=action, message_id=glacier_id, folder_id=glacier_folder_id,
        )

    if action == "expunge":
        if not request.confirm:
            return MessageActionResponse(
                success=False, action=action, message_id=glacier_id, applied=False,
                folder_id=glacier_folder_id,
                message="This is the only copy of this message. Confirm to delete it "
                "permanently.",
            )
        async with db.session() as session:
            await session.execute(
                text("DELETE FROM glacier_attachments WHERE glacier_message_id = :id"),
                {"id": glacier_id},
            )
            # A tombstone, not a physical delete -- the same shape a
            # completed restore leaves (visible_at cleared, bytes
            # nulled), state='expunged' distinguishing "destroyed on
            # purpose" from "restored to the server". Without this the
            # id resolves to nothing, and every read path falls back to
            # the stale `messages` row (kept deliberately readable for
            # an ordinary client-side move) as if this message had
            # merely moved elsewhere rather than been destroyed.
            await session.execute(
                text(
                    "UPDATE glacier_messages SET state = 'expunged', visible_at = NULL, "
                    "raw_source = NULL, body_text = NULL, body_html = NULL, "
                    "raw_headers = NULL WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        return MessageActionResponse(
            success=True, action=action, message_id=glacier_id, message="Permanently deleted",
        )

    if action in ("move", "archive", "trash"):
        if action == "move":
            if request.target_folder_id is None:
                raise HTTPException(status_code=400, detail="target_folder_id required for move")
            target_folder_id = request.target_folder_id
            async with db.session() as session:
                is_another_glacier = (
                    await session.execute(
                        select(AccountPrefs.account_id).where(
                            AccountPrefs.glacier_folder_id == target_folder_id,
                        )
                    )
                ).scalar_one_or_none()
                if is_another_glacier is not None:
                    return MessageActionResponse(
                        success=False, action=action, message_id=glacier_id, applied=False,
                        folder_id=glacier_folder_id, message=GLACIER_REFUSAL_REASON,
                    )
                if not await _folder_belongs_to_account(session, row.account_id, target_folder_id):
                    raise HTTPException(
                        status_code=400, detail="target_folder_id does not belong to this account",
                    )
        else:
            resolved = await _resolve_special_folder(row.account_id, action)
            if resolved is None:
                raise HTTPException(
                    status_code=400, detail=f"No {action} folder found for this account",
                )
            target_folder_id = resolved
        outcome = await start_restore(db, glacier_id, target_folder_id)
        return MessageActionResponse(
            success=outcome.ok, action=action, message_id=glacier_id, message=outcome.reason,
            applied=outcome.ok, folder_id=glacier_folder_id,
        )

    if action in ("spam", "not_spam"):
        return MessageActionResponse(
            success=False, action=action, message_id=glacier_id, applied=False,
            folder_id=glacier_folder_id,
            message="Spam rulings on glaciered mail are not yet supported.",
        )

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


async def _announce_glacier_change(
    account_id: uuid.UUID, glacier_id: uuid.UUID, folder_id: uuid.UUID,
) -> None:
    """glacier_messages carries no PostIMAP trigger of its own -- a write
    to a MailVerdict-owned table announces nothing unless the write path
    pushes its own event, the same rule every other owned table in this
    codebase follows (see docs/architecture.md)."""
    event_ring = get_event_ring()
    if event_ring is not None:
        await event_ring.add(
            account_id, "mail.updated",
            {"id": str(glacier_id), "account_id": str(account_id), "folder_id": str(folder_id)},
        )


async def _action_target(
    account_id: uuid.UUID, action: str, target_folder_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """The folder an action files messages into, if it files them anywhere:
    the named target of a move, the account's role folder otherwise."""
    if action == "move":
        return target_folder_id
    role = {"archive": "archive", "trash": "trash", "spam": "junk", "not_spam": "inbox"}.get(action)
    return await _resolve_special_folder(account_id, role) if role else None


_FLAG_ACTIONS = frozenset({"mark_read", "mark_unread", "flag", "unflag", "expunge"})


def _not_applied(action: str, message_id: uuid.UUID) -> MessageActionResponse:
    """The answer to a guarded action whose message is no longer where the
    caller saw it -- a success, since nothing is wrong and nothing should be
    retried, that wrote nothing."""
    return MessageActionResponse(
        success=True, action=action, message_id=message_id, applied=False,
        message="Not applied: the message is no longer in the expected folder",
    )


async def _handle_spam_action(
    message_id: uuid.UUID, account_id: uuid.UUID, *, is_spam: bool,
    expected_folder_id: uuid.UUID | None = None,
) -> MessageActionResponse:
    """
    Record the user's ruling and move the message to match, in one call
    through SpamFeedbackHandler.apply_human_ruling -- see its own
    docstring for exactly what moves and when, and for why this no
    longer relies on the folder-move listener to catch the 'spam'
    direction. Raises the same 400 archive/trash already raise when the
    account has no folder to move into -- the ruling is recorded either
    way, but "moved to Junk" would otherwise be reported for a message
    that never moved.
    """
    from mail_verdict.server import get_spam_processor
    from mail_verdict.spam.feedback import FolderResolutionError

    processor = get_spam_processor()
    if processor is None:
        raise HTTPException(status_code=503, detail="Spam feedback handler not available")
    try:
        ok = await processor.feedback.apply_human_ruling(
            message_id, account_id, is_spam=is_spam, expected_folder_id=expected_folder_id,
        )
    except FolderResolutionError as exc:
        raise HTTPException(
            status_code=400, detail=f"No {exc.role} folder found for this account",
        ) from exc

    action = "spam" if is_spam else "not_spam"
    return MessageActionResponse(
        success=ok, action=action, message_id=message_id,
        message=(
            ("Marked as spam" if is_spam else "Marked as not spam")
            if ok else "Feedback processing failed"
        ),
    )


# Mail filed here by the user is marked read as it moves --
# settings.mail.mark_read_on_file_to_archive_or_junk. Narrower than
# pipeline/enqueue.py's own _SKIP_FOLDER_SPECIAL_USE on purpose: this is
# about where the user just filed something, not about pipeline scope.
_MARK_READ_SPECIAL_USE = frozenset({"archive", "junk"})


def _should_mark_read_on_file(target_role: str | None) -> bool:
    """
    Whether a move into a folder with this effective special_use should
    mark the moved message(s) read -- the toolbar Archive action and a
    drag-and-drop move both resolve to the "move"/"archive" handlers
    below, so this is the one place that decides it, not two.

    Deliberately not an on-move pipeline rule: the pipeline only ever
    triggers on arrival (see pipeline/enqueue.py's own docstring on why),
    and this is about a move the user just made, not something the
    pipeline should ever re-evaluate. The AI spam stage already marks its
    own moves to Junk read; this is the same folder-role condition
    applied to the user's own moves instead.

    Args:
        target_role: The move's target folder's effective special_use, or
            None if it isn't one of PostIMAP's or folder_prefs' known roles

    Returns:
        False immediately if target_role isn't archive or junk, without
        even reading the setting -- so a plain move to an ordinary folder
        never pays for a settings lookup it doesn't need.
    """
    if target_role not in _MARK_READ_SPECIAL_USE:
        return False
    settings = get_settings_service().get("mail")
    return bool(settings.get("mark_read_on_file_to_archive_or_junk", True))


async def _resolve_special_folder(account_id: uuid.UUID, role: str) -> uuid.UUID | None:
    """
    Resolve a special folder UUID by its effective special_use.

    folder_prefs.special_use_override exists for servers that don't
    advertise SPECIAL-USE -- matching only the raw Folder.special_use (as
    list_folders does not) means every trash/archive/spam action fails with
    "no trash folder found" on exactly the servers the override is for.

    Args:
        account_id: Account to look up
        role: Folder role key (e.g., "archive", "junk", "trash", "inbox")

    Returns:
        Folder UUID or None if not found
    """
    return await FolderRepository(get_db_connection()).resolve_special_folder(account_id, role)


async def _folder_belongs_to_account(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> bool:
    """
    A move's target folder is client-supplied and never otherwise checked
    against the message's own account -- without this, a client can move
    a message into another account's folder, since move_message() and
    move_message_bulk() write folder_id with no ownership check of their
    own (they trust the caller, same as every other postimap/actions.py
    helper).

    Args:
        session: Active AsyncSession
        account_id: The account the message being moved belongs to
        folder_id: The client-supplied target folder

    Returns:
        True if `folder_id` exists and belongs to `account_id`
    """
    result = await session.execute(
        select(Folder.id).where(Folder.id == folder_id, Folder.account_id == account_id)
    )
    return result.scalar_one_or_none() is not None


async def _glacier_account_for_folder(
    session: AsyncSession, folder_id: uuid.UUID,
) -> uuid.UUID | None:
    """The account an enabled glacier folder belongs to, or None when
    `folder_id` does not name one -- a glacier folder is never a row in
    `folders` at all, so `_folder_belongs_to_account` above always
    answers False for it and every move-target check (single action,
    bulk action, restore's own move-into-another-glacier refusal) needs
    this one first, before deciding whether the ordinary check even
    applies."""
    result = await session.execute(
        select(AccountPrefs.account_id).where(
            AccountPrefs.glacier_folder_id == folder_id, AccountPrefs.glacier_enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


def _refuse_over_manual_batch_cap(count: int) -> None:
    """`glacier.max_manual_batch` (config/config.yaml) is this feature's
    own pacing cap for a person's own bulk action -- the account-wide
    automatic sweep paces itself independently (glacier/sweep.py's
    batch_size), so this exists purely to stop a "select all" over a
    huge archive from queueing thousands of IMAP writes into PostIMAP's
    outbound queue at once, ahead of every other flag change and send on
    the account. Applies to moving mail INTO the glacier and to
    restoring it back OUT -- both drive a real IMAP write per message;
    every other glacier action (marking, keywords, permanent delete) is
    local to this database and needs no such cap.

    Args:
        count: How many messages this bulk request would act on

    Raises:
        HTTPException: 409, naming the cap and how many were asked for
    """
    cap = get_config().glacier.max_manual_batch
    if count > cap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A bulk glacier move or restore is capped at {cap} message(s) at once; "
                f"this would act on {count}. Narrow the selection and try again."
            ),
        )


async def _bulk_glacier_move(
    db: DatabaseConnection, groups: dict[uuid.UUID | None, list[uuid.UUID]],
) -> tuple[int, int, list[str], list[uuid.UUID]]:
    """Glacier every message in `groups`, one at a time -- glacier_message_
    now is a whole copy/verify/expunge sequence of its own transactions,
    not a single UPDATE, so unlike move_groups() there is no batched
    statement to fall back to. A guarded group (expected is not None) is
    filtered down to the ids actually still in that folder first, the
    same "already moved elsewhere since the caller last looked" check
    move_messages_from() makes for an ordinary bulk move -- the rest are
    reported skipped rather than attempted.

    Returns:
        (how many actually left the server -- a resolved duplicate
        included, since its server copy genuinely was removed --
        how many of those were duplicates rather than newly copied, the
        distinct FAILURE reasons hit, and ids left alone because they
        had already moved elsewhere)
    """
    landed = 0
    duplicate_count = 0
    reasons: dict[str, None] = {}
    skipped: list[uuid.UUID] = []
    for expected, ids in groups.items():
        eligible = ids
        if expected is not None:
            async with db.session() as session:
                still_there = await session.execute(
                    select(Message.id).where(
                        Message.id.in_(ids), Message.folder_id == expected,
                        Message.expunged_at.is_(None),
                    )
                )
                eligible = list(still_there.scalars())
            eligible_set = set(eligible)
            skipped.extend(mid for mid in ids if mid not in eligible_set)
        for mid in eligible:
            outcome = await glacier_message_now(db, mid, event_ring=get_event_ring())
            if outcome.ok:
                landed += 1
                # A success can still carry a reason (a duplicate whose
                # server copy was removed rather than newly copied) --
                # counted, never mistaken for a failure by going into
                # `errors`, which decides response.success.
                if outcome.reason is not None:
                    duplicate_count += 1
            elif outcome.reason is not None:
                reasons.setdefault(outcome.reason, None)
    return landed, duplicate_count, list(reasons), skipped


@account_router.get("/selection", response_model=SelectionSnapshotResponse)
async def mint_selection(
    account_id: uuid.UUID,
    folder_id: uuid.UUID = Query(...),
    filter: Literal["unread", "all"] = Query(default="all"),  # noqa: A002
) -> SelectionSnapshotResponse:
    """
    Mint a 'select all matching' snapshot: the current instant and the
    predicate's count at that instant, from one statement so the two can
    never disagree. Side-effect free -- no selection state is created here,
    the client holds the returned descriptor and sends it back on the
    bulk-action request that acts on it.
    """
    db = get_db_connection()
    async with db.session() as session:
        glacier_account_id = await _glacier_account_for_folder(session, folder_id)
        if glacier_account_id == account_id:
            # The glacier folder id is synthetic -- never a row in
            # `messages` (design section 2) -- so the live count below
            # always answers zero for it, and a "select all" snapshot
            # minted from that zero would make every bulk action against
            # the whole glacier refuse itself as unconfirmed.
            stmt = select(func.now(), func.count(GlacierMessage.id)).where(
                GlacierMessage.account_id == account_id,
                GlacierMessage.visible_at.is_not(None),
            )
            if filter == "unread":
                stmt = stmt.where(GlacierMessage.is_seen.is_(False))
        else:
            stmt = select(func.now(), func.count(Message.id)).where(
                Message.account_id == account_id,
                Message.folder_id == folder_id,
                Message.expunged_at.is_(None),
            )
            if filter == "unread":
                stmt = stmt.where(Message.is_seen.is_(False))
        row = (await session.execute(stmt)).one()
    return SelectionSnapshotResponse(snapshot_at=row[0], count=row[1])


@account_router.post("/bulk-action", response_model=BulkActionResponse)
async def bulk_action(account_id: uuid.UUID, request: BulkActionRequest) -> BulkActionResponse:
    """
    Apply one action to many messages, selected by an id list, a scope, or
    both -- a predicate scope plus explicit ids added on top of it (a row
    outside the predicate the user ticked by hand).

    A scope resolves server-side ("everything unread in this folder") so a
    virtualized, never-fully-fetched list can still "select all" without
    the client holding every id. A request carrying an idempotency_key is
    applied once however often it is repeated (mail_actions/submissions.py).
    """
    if request.idempotency_key is None:
        return await _apply_bulk_action(account_id, request)
    return await run_once(
        get_db_connection(),
        request.idempotency_key,
        request_fingerprint("bulk", account_id, request),
        BulkActionResponse,
        lambda: _apply_bulk_action(account_id, request),
    )


async def _apply_bulk_action(
    account_id: uuid.UUID, request: BulkActionRequest,
) -> BulkActionResponse:
    """Apply one bulk action -- bulk_action without its key.

    Every write is made per group of messages sharing the folder the caller
    expected them in (expected_folder_ids), guarded to that folder, so a
    message moved elsewhere between the check and the write is still left
    alone. Messages with no expectation (a scope, or ids not named there)
    form one unguarded group.
    """
    db = get_db_connection()

    # Glacier ids live in a different table with ids disjoint from
    # `messages`, so nothing below can ever resolve or act on one --
    # resolved and applied through their own path first (marking,
    # restoring to a server folder, expunging for good all already exist
    # per-message in _apply_glacier_message_action; there is no batched
    # statement for any of them the way move_groups() has for live rows).
    glacier_ids: list[uuid.UUID] = []
    async with db.session() as session:
        glacier_folder_id = await _glacier_folder_id_for_account(session, account_id)
        if glacier_folder_id is not None:
            if request.scope is not None and request.scope.folder_id == glacier_folder_id:
                glacier_ids.extend(
                    await _resolve_glacier_scope_ids(session, account_id, request.scope)
                )
            if request.ids:
                glacier_ids.extend(
                    await _resolve_glacier_explicit_ids(session, account_id, request.ids)
                )
    glacier_ids = list(dict.fromkeys(glacier_ids))
    glacier_id_set = set(glacier_ids)

    glacier_affected = 0
    glacier_errors: list[str] = []
    glacier_skipped: list[uuid.UUID] = []
    if glacier_ids:
        if request.action in ("move", "archive", "trash"):
            _refuse_over_manual_batch_cap(len(glacier_ids))
        glacier_affected, glacier_errors, glacier_skipped = await _bulk_glacier_action(
            db, glacier_ids, request,
        )

    live_ids = [mid for mid in request.ids if mid not in glacier_id_set] if request.ids else None

    sources: list[BulkActionSource] = []
    skipped: list[uuid.UUID] = list(glacier_skipped)
    # message id -> the folder its write is guarded to, None for unguarded
    expected_of: dict[uuid.UUID, uuid.UUID | None] = {}
    async with db.session() as session:
        if request.scope is not None:
            for mid in await _resolve_scope_ids(session, account_id, request.scope):
                expected_of[mid] = None
        if live_ids:
            # An explicit id list is client-supplied and otherwise never
            # checked against the path's account_id -- narrowed to the
            # ids that actually belong here (and still exist) the same
            # way a scope already is, rather than trusting the list.
            live = await _resolve_explicit_ids(session, account_id, live_ids)
            guards = request.expected_folder_ids or {}
            target = (
                await _action_target(account_id, request.action, request.target_folder_id)
                if guards else None
            )
            explicit: list[uuid.UUID] = []
            for mid in dict.fromkeys(live_ids):
                folder = live.get(mid)
                if folder is not None and mid in guards and guards[mid] != folder:
                    # Already where the action files it: done, not a miss
                    # (see _apply_message_action).
                    if folder != target:
                        skipped.append(mid)
                elif folder is None:
                    skipped.append(mid)
                else:
                    explicit.append(mid)
            members: list[tuple[uuid.UUID, uuid.UUID]] | None = None
            if request.expand_threads and request.action != "expunge":
                members = await _expand_to_conversations(
                    session, account_id, explicit, request.expand_threads_through,
                )
                sources = [BulkActionSource(id=mid, folder_id=fid) for mid, fid in members]
            for mid, expected in _write_guards(explicit, live, guards, members).items():
                expected_of.setdefault(mid, expected)
        message_ids = list(expected_of)

    # A caller that showed a count to a user before sending this request
    # (an "empty this folder" confirmation, most concretely) repeats it
    # back here -- checked against what actually resolves now, not what
    # was true when it was minted. Mirrors folder deletion's own
    # confirm_message_count gate: a stale or optimistically-adjusted count
    # must not be able to make an irreversible write look confirmed when
    # it wasn't. Most actions pass nothing and skip this entirely.
    confirmed = request.confirm_message_count
    total_resolved = len(message_ids) + len(glacier_ids)
    if confirmed is not None and confirmed != total_resolved:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This resolves to {total_resolved} message(s) now, not the "
                f"{confirmed} confirmed. Repeat the request "
                f"with confirm_message_count={total_resolved} to proceed."
            ),
        )

    if not message_ids and not glacier_ids:
        return BulkActionResponse(
            success=True, action=request.action, affected_count=0, skipped_ids=skipped,
        )

    groups: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for mid, expected in expected_of.items():
        groups.setdefault(expected, []).append(mid)

    action = request.action
    errors: list[str] = list(glacier_errors)
    affected = glacier_affected
    duplicate_count = 0
    target = None

    async def move_groups(session: AsyncSession, target: uuid.UUID) -> list[uuid.UUID]:
        """Move every group into `target`; the ids that moved or were
        already there."""
        nonlocal affected
        landed: list[uuid.UUID] = []
        for expected, ids in groups.items():
            if expected is None:
                affected += await move_message_bulk(session, ids, target)
                landed.extend(ids)
            elif expected == target:
                landed.extend(ids)
            else:
                moved = await move_messages_from(session, ids, expected, target)
                affected += len(moved)
                landed.extend(moved)
                moved_set = set(moved)
                skipped.extend(mid for mid in ids if mid not in moved_set)
        return landed

    if action in ("mark_read", "mark_unread", "flag", "unflag"):
        column = "is_seen" if action in ("mark_read", "mark_unread") else "is_flagged"
        value = action in ("mark_read", "flag")
        async with db.session() as session:
            for expected, ids in groups.items():
                affected += await set_flags_bulk(
                    session, ids, expected_folder_id=expected, **{column: value},
                )
    elif action == "expunge":
        async with db.session() as session:
            for expected, ids in groups.items():
                affected += await expunge_bulk(session, ids, expected_folder_id=expected)
    elif action in ("move", "archive", "trash"):
        if action == "move":
            if not request.target_folder_id:
                raise HTTPException(status_code=400, detail="target_folder_id required for move")
            target = request.target_folder_id
            target_role = await FolderRepository(db).get_effective_special_use(
                request.target_folder_id,
            )
        else:
            target_role = action
            target = await _resolve_special_folder(account_id, action)
            if target is None:
                errors.append(f"No {action} folder found for this account")
        if target is not None:
            glacier_account_id = None
            if action == "move":
                async with db.session() as session:
                    glacier_account_id = await _glacier_account_for_folder(session, target)
            if glacier_account_id is not None:
                if glacier_account_id != account_id:
                    raise HTTPException(
                        status_code=400, detail="target_folder_id does not belong to this account",
                    )
                _refuse_over_manual_batch_cap(sum(len(ids) for ids in groups.values()))
                affected, duplicate_count, glacier_errors, glacier_skipped = (
                    await _bulk_glacier_move(db, groups)
                )
                errors.extend(glacier_errors)
                skipped.extend(glacier_skipped)
            else:
                async with db.session() as session:
                    if action == "move" and not await _folder_belongs_to_account(
                        session, account_id, target,
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="target_folder_id does not belong to this account",
                        )
                    landed = await move_groups(session, target)
                    if landed and _should_mark_read_on_file(target_role):
                        await set_flags_bulk(session, landed, is_seen=True)
    elif action in ("spam", "not_spam"):
        from mail_verdict.server import get_spam_processor
        from mail_verdict.spam.feedback import FolderResolutionError

        target = await _resolve_special_folder(account_id, "junk" if action == "spam" else "inbox")
        processor = get_spam_processor()
        if processor is None:
            errors.append("Spam feedback handler not available")
        else:
            # One call per message rather than a bulk move: each
            # message's ruling is recorded and applied together through
            # the same function every other surface calls (see
            # SpamFeedbackHandler.apply_human_ruling), not a bulk move
            # with the feedback bolted on separately. affected counts
            # only rulings that actually moved -- one message whose
            # verdict couldn't be recorded, or whose account has no
            # folder to move it into, must not be reported as moved
            # alongside the rest.
            succeeded = 0
            missing_role: str | None = None
            for mid in message_ids:
                try:
                    ok = await processor.feedback.apply_human_ruling(
                        mid, account_id, is_spam=(action == "spam"),
                        expected_folder_id=expected_of[mid],
                    )
                except FolderResolutionError as exc:
                    missing_role = exc.role
                    continue
                if ok:
                    succeeded += 1
            affected = succeeded
            if missing_role is not None:
                errors.append(f"No {missing_role} folder found for this account")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    return BulkActionResponse(
        success=not errors, action=action, affected_count=affected, errors=errors,
        sources=sources, skipped_ids=skipped, duplicate_count=duplicate_count,
        target_folder_id=target if action not in _FLAG_ACTIONS else None,
    )


def _write_guards(
    explicit: list[uuid.UUID],
    live: dict[uuid.UUID, uuid.UUID],
    guards: dict[uuid.UUID, uuid.UUID],
    members: list[tuple[uuid.UUID, uuid.UUID]] | None,
) -> dict[uuid.UUID, uuid.UUID | None]:
    """
    The folder each message's write is guarded to, None for unguarded.

    Args:
        explicit: The named ids that passed the read-time check
        live: Each named id's folder as just read
        guards: The caller's expected_folder_ids
        members: With expand_threads, every conversation member and its
            folder; None otherwise

    Returns:
        message id -> the folder its write must still find it in. A
        conversation member is guarded to its anchor's folder -- it shares
        that folder, and a member moved away between the read and the write
        must be left there as much as the anchor.
    """
    if members is None:
        return {mid: (live[mid] if mid in guards else None) for mid in explicit}
    guarded_folders = {live[mid] for mid in explicit if mid in guards}
    return {mid: (fid if fid in guarded_folders else None) for mid, fid in members}


async def _expand_to_conversations(
    session: AsyncSession,
    account_id: uuid.UUID,
    ids: list[uuid.UUID],
    mirrored_through: datetime | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """
    Every live message sharing a conversation with one of `ids` and sitting
    in that id's own folder, with the folder it is in -- what a
    conversation row in a grouped list stands for. The ids themselves are
    included. Messages of the same conversation in other folders (the
    reader's own replies in Sent, say) are left where they are, and so is
    any member mirrored after `mirrored_through` when one is given.
    """
    if not ids:
        return []
    anchors = (
        await session.execute(
            select(Message.thread_id, Message.folder_id).where(
                Message.id == any_(ids), Message.account_id == account_id,  # type: ignore[arg-type]
            )
        )
    ).all()
    pairs = {(t, f) for t, f in anchors if t is not None}
    members: dict[uuid.UUID, uuid.UUID] = {}
    if pairs:
        stmt = select(Message.id, Message.folder_id).where(
            Message.account_id == account_id,
            Message.expunged_at.is_(None),
            tuple_(Message.thread_id, Message.folder_id).in_(list(pairs)),
        )
        if mirrored_through is not None:
            stmt = stmt.where(Message.created_at <= mirrored_through)
        rows = (await session.execute(stmt)).all()
        members.update({mid: fid for mid, fid in rows})
    # A message with no conversation still stands for itself.
    for mid, fid in (
        await session.execute(
            select(Message.id, Message.folder_id).where(
                Message.id == any_(ids), Message.account_id == account_id,  # type: ignore[arg-type]
                Message.expunged_at.is_(None),
            )
        )
    ).all():
        members.setdefault(mid, fid)
    return list(members.items())


async def _resolve_explicit_ids(
    session: AsyncSession, account_id: uuid.UUID, ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """
    Narrow a client-supplied id list to the ones that actually belong to
    this account and still exist, with the folder each is in.

    Without this, a bulk action's explicit-id path (unlike its scope
    path, which is already account-scoped by construction) acts on
    whatever ids a client names -- including one belonging to a different
    account, or one already expunged. An id that fails either check is
    silently dropped rather than acted on; affected_count then reflects
    the resolved subset, not the length of what was asked for.

    Matched with `= ANY(:ids)` rather than `IN (...)`: an IN clause binds
    one parameter per id, and asyncpg refuses a statement over 32767
    parameters -- exactly what a large "select all in this folder" client
    can send. ANY binds the whole list as a single array parameter.
    """
    if not ids:
        return {}
    result = await session.execute(
        select(Message.id, Message.folder_id).where(
            Message.id == any_(ids), Message.account_id == account_id,  # type: ignore[arg-type]
            Message.expunged_at.is_(None),
        )
    )
    return {mid: fid for mid, fid in result.all()}


async def _resolve_scope_ids(
    session: AsyncSession, account_id: uuid.UUID, scope: BulkActionScope,
) -> list[uuid.UUID]:
    """
    Resolve a bulk-action scope descriptor to a concrete list of message ids.

    `created_at <= snapshot_at` excludes anything mirrored after the client
    minted this scope -- the guard against sweeping in mail that arrived
    between "select all" and the button press, which the user never agreed
    to and never saw. exclude_ids is matched with `!= ALL(:ids)` rather
    than `NOT IN (...)` for the same reason as _resolve_explicit_ids's
    `= ANY(:ids)`.
    """
    stmt = select(Message.id).where(
        Message.account_id == account_id,
        Message.folder_id == scope.folder_id,
        Message.expunged_at.is_(None),
        Message.created_at <= scope.snapshot_at,
    )
    if scope.filter == "unread":
        stmt = stmt.where(Message.is_seen.is_(False))
    if scope.exclude_ids:
        stmt = stmt.where(Message.id != all_(scope.exclude_ids))  # type: ignore[arg-type]
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _glacier_folder_id_for_account(
    session: AsyncSession, account_id: uuid.UUID,
) -> uuid.UUID | None:
    """The synthetic folder id this account's glacier answers to, or None
    when it has none enabled -- the reverse direction of
    _glacier_account_for_folder, for resolving a bulk action's own
    account_id into the one folder id a glacier-scoped selection could
    possibly name."""
    result = await session.execute(
        select(AccountPrefs.glacier_folder_id).where(
            AccountPrefs.account_id == account_id, AccountPrefs.glacier_enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _resolve_glacier_explicit_ids(
    session: AsyncSession, account_id: uuid.UUID, ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Which of `ids` name a visible glacier row on this account -- the
    glacier counterpart of _resolve_explicit_ids, since `messages` and
    `glacier_messages` are different tables with disjoint ids and an
    explicit selection can mix ids from either. Matches a glacier row's
    own id as well as its origin_message_id (the pre-glacier id a caller
    may still hold, the same identity resolve_glacier_id checks), and
    returns each match exactly as it appeared in `ids` -- the caller uses
    this list for its own id-set bookkeeping, not to look the row up
    again, and _bulk_glacier_action resolves each one properly itself
    when it actually acts on it."""
    if not ids:
        return []
    result = await session.execute(
        select(GlacierMessage.id, GlacierMessage.origin_message_id).where(
            or_(
                GlacierMessage.id == any_(ids),  # type: ignore[arg-type]
                GlacierMessage.origin_message_id == any_(ids),  # type: ignore[arg-type]
            ),
            GlacierMessage.account_id == account_id, GlacierMessage.visible_at.is_not(None),
        )
    )
    id_set = set(ids)
    matched: list[uuid.UUID] = []
    for glacier_id, origin_id in result.all():
        if glacier_id in id_set:
            matched.append(glacier_id)
        elif origin_id is not None and origin_id in id_set:
            matched.append(origin_id)
    return matched


async def _resolve_glacier_scope_ids(
    session: AsyncSession, account_id: uuid.UUID, scope: BulkActionScope,
) -> list[uuid.UUID]:
    """The glacier counterpart of _resolve_scope_ids -- visible_at is what
    a glacier row's own snapshot instant is, the moment it stopped being
    a live row and started being this one (GlacierMessage's own
    docstring); its own created_at is copied from the live row instead
    and can be years old, so using it here the way _resolve_scope_ids
    uses Message.created_at would sweep in glacier rows regardless of
    when the caller actually saw them."""
    stmt = select(GlacierMessage.id).where(
        GlacierMessage.account_id == account_id,
        GlacierMessage.folder_id == scope.folder_id,
        GlacierMessage.visible_at.is_not(None),
        GlacierMessage.visible_at <= scope.snapshot_at,
    )
    if scope.filter == "unread":
        stmt = stmt.where(GlacierMessage.is_seen.is_(False))
    if scope.exclude_ids:
        stmt = stmt.where(GlacierMessage.id != all_(scope.exclude_ids))  # type: ignore[arg-type]
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _bulk_glacier_action(
    db: DatabaseConnection, glacier_ids: list[uuid.UUID], request: BulkActionRequest,
) -> tuple[int, list[str], list[uuid.UUID]]:
    """Apply one bulk action to a selection of already-glaciered messages,
    one at a time through _apply_glacier_message_action -- move/restore is
    a whole outbox append and expunge deletes attachment rows too, so
    unlike move_groups() there is no batched statement for any of this.

    Returns:
        (how many actually applied, the distinct failure reasons hit,
        ids that had already been restored or expunged by something else
        between resolution and this call)
    """
    landed = 0
    reasons: dict[str, None] = {}
    gone: list[uuid.UUID] = []
    for gid in glacier_ids:
        action_request = MessageActionRequest(
            action=request.action, target_folder_id=request.target_folder_id,
            confirm=request.confirm,
        )
        response = await _apply_glacier_message_action(gid, action_request)
        if response is None:
            gone.append(gid)
        elif response.success:
            landed += 1
        elif response.message is not None:
            reasons.setdefault(response.message, None)
    return landed, list(reasons), gone
