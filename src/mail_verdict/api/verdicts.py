"""
Verdict API endpoints.

GET /api/verdicts — verdict history
GET /api/verdicts/spam-review — undecided spam verdicts, paged
GET /api/mails/:id/verdict — latest verdict for a message
POST /api/mails/:id/feedback — submit user spam feedback
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import aliased

from mail_verdict.api.deps import get_message_repo, get_verdict_repo
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.mails import _LIST_DEFERRED_COLUMNS
from mail_verdict.api.schemas import (
    FeedbackRequest,
    FeedbackResponse,
    SpamReviewItem,
    SpamReviewListResponse,
    VerdictResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Folder, Message, Verdict, VerdictSource

logger = logging.getLogger(__name__)

router = APIRouter(tags=["verdicts"])


@router.get("/verdicts", response_model=list[VerdictResponse])
async def list_verdicts(
    account_id: uuid.UUID | None = Query(default=None),
    mail_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[VerdictResponse]:
    """List verdicts with optional filters."""
    db = get_db_connection()
    async with db.session() as session:
        stmt = select(Verdict).order_by(desc(Verdict.created_at))

        if mail_id is not None:
            stmt = stmt.where(Verdict.mail_id == mail_id)
        if account_id is not None:
            stmt = stmt.join(Message, Verdict.mail_id == Message.id).where(
                Message.account_id == account_id,
            )

        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        verdicts = list(result.scalars().all())

    return [
        VerdictResponse(
            id=v.id,
            message_id=v.mail_id,
            is_spam=v.is_spam,
            model_used=v.model_used,
            reasoning=v.reasoning,
            source=v.source.value,
            created_at=v.created_at,
        )
        for v in verdicts
    ]


@router.get("/verdicts/spam-review", response_model=SpamReviewListResponse)
async def list_spam_review(
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: id of the last verdict in the previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> SpamReviewListResponse:
    """
    Every message whose latest verdict calls it spam, with no user ruling
    since -- across every account and folder, including Junk, since this
    is a triage view over verdicts rather than a folder listing; nothing
    here moves a message or changes the classification pipeline.

    "No user ruling since" is exactly "the latest verdict is itself not a
    user_feedback row" -- a user's thumb-up (agreeing it's spam) inserts a
    USER_FEEDBACK verdict the same as a thumb-down does, so a message the
    latest verdict for which is USER_FEEDBACK has already been decided
    either way and drops out, regardless of that row's own is_spam value.

    DISTINCT ON picks the latest verdict per message, the same shape the
    threaded mail list uses for one row per thread; DISTINCT ON's own
    ORDER BY has to start with mail_id, so re-ordering by the verdict's own
    created_at for cursor pagination is an outer step, not folded into it.
    """
    db = get_db_connection()
    async with db.session() as session:
        cursor_created_at, cursor_id = None, None
        if before is not None:
            cursor_result = await session.execute(
                select(Verdict.created_at, Verdict.id).where(Verdict.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: verdict {before} not found",
                )
            cursor_created_at, cursor_id = cursor_row

        latest_per_mail = (
            select(Verdict)
            .distinct(Verdict.mail_id)
            .order_by(Verdict.mail_id, desc(Verdict.created_at), desc(Verdict.id))
            .subquery("latest_per_mail")
        )
        latest = aliased(Verdict, latest_per_mail)

        stmt = (
            select(Message, latest, Folder.special_use)
            .options(*_LIST_DEFERRED_COLUMNS)
            .join(latest, latest.mail_id == Message.id)
            .join(Folder, Folder.id == Message.folder_id)
            .where(
                Message.expunged_at.is_(None),
                latest.is_spam.is_(True),
                latest.source != VerdictSource.USER_FEEDBACK,
            )
            .order_by(desc(latest.created_at), desc(latest.id))
        )
        if cursor_id is not None:
            stmt = stmt.where(
                or_(
                    latest.created_at < cursor_created_at,
                    and_(latest.created_at == cursor_created_at, latest.id < cursor_id),
                )
            )
        stmt = stmt.limit(limit + 1)

        result = await session.execute(stmt)
        rows = result.all()

    has_more = len(rows) > limit
    items = [
        SpamReviewItem(
            message_id=m.id,
            account_id=m.account_id,
            folder_id=m.folder_id,
            is_junk=special_use == "junk",
            subject=m.subject,
            from_addr=m.from_addr,
            received_at=m.received_at,
            snippet=m.body_text[:120] if m.body_text else None,
            verdict_id=v.id,
            model_used=v.model_used,
            reasoning=v.reasoning,
            verdict_created_at=v.created_at,
        )
        for m, v, special_use in rows[:limit]
    ]
    next_cursor = str(items[-1].verdict_id) if has_more and items else None
    return SpamReviewListResponse(items=items, has_more=has_more, next_cursor=next_cursor)


@router.get("/mails/{mail_id}/verdict", response_model=VerdictResponse | None)
async def get_message_verdict(
    mail_id: uuid.UUID,
) -> VerdictResponse | None:
    """Get the latest verdict for a specific message."""
    verdict_repo = get_verdict_repo()
    verdict = await verdict_repo.get_latest_for_mail(mail_id)
    if verdict is None:
        return None

    return VerdictResponse(
        id=verdict.id,
        message_id=verdict.mail_id,
        is_spam=verdict.is_spam,
        model_used=verdict.model_used,
        reasoning=verdict.reasoning,
        source=verdict.source.value,
        created_at=verdict.created_at,
    )


@router.post("/mails/{mail_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    mail_id: uuid.UUID,
    request: FeedbackRequest,
    account_id: uuid.UUID = Query(),
) -> FeedbackResponse:
    """
    Submit user feedback on spam classification.

    Triggers SpamFeedbackHandler to log a correction verdict.
    """
    msg_repo = get_message_repo()
    msg = await msg_repo.get_by_id(account_id, mail_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Access SpamFeedbackHandler from server state
    from mail_verdict.server import get_spam_processor

    processor = get_spam_processor()
    if processor is None:
        raise HTTPException(status_code=503, detail="Spam feedback handler not available")

    feedback = processor.feedback
    if request.is_spam:
        ok = await feedback.handle_moved_to_spam(mail_id, account_id)
    else:
        ok = await feedback.handle_moved_from_spam(mail_id, account_id)

    # A correction changes what every viewer of this message should see
    # (the verdict badge, the reasoning), not only the browser that
    # submitted it -- the same event the AI pipeline's own verdict fires,
    # so a listener never needs to tell the two sources apart.
    event_ring = get_event_ring()
    if ok and event_ring is not None:
        await event_ring.add(
            account_id, "verdict.issued",
            {
                "message_id": str(mail_id), "is_spam": request.is_spam,
                "source": "user_feedback", "account_id": str(account_id),
            },
        )

    return FeedbackResponse(
        success=ok,
        message_id=mail_id,
        is_spam=request.is_spam,
        message="Feedback recorded" if ok else "Feedback processing failed",
    )
