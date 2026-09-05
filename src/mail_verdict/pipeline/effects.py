"""
Applying a stage's declared effects.

Every effect maps to exactly one call in postimap/actions.py or one write
to a MailVerdict-owned table, and every one of those calls is guarded: the
rowcount is the only thing believed. A guard that fails does not raise --
the message was expunged or moved between the run reading the world and
applying its effect, which is an ordinary race, not a bug -- it produces
an AppliedEffect the runner records as "not applied" and folds into a
`skipped` run rather than a `done` one.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import MailTag, TagSource
from mail_verdict.pipeline.contracts import (
    Effect,
    Expunge,
    Keywords,
    Move,
    Notify,
    RecordVerdict,
    SetFlags,
    StageMisconfigured,
    Tag,
    Trash,
)
from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.postimap.actions import (
    expunge_guarded,
    move_message_guarded,
    set_flags_guarded,
    set_keywords_delta_guarded,
)

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.pipeline.context import FolderResolver

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppliedEffect:
    """What actually happened when one effect was applied -- or would have
    happened, in a dry run."""

    effect: Effect
    applied: bool
    detail: str


async def apply_effects(
    db: DatabaseConnection,
    view: MessageView,
    effects: tuple[Effect, ...],
    *,
    apply: bool,
    folders: FolderResolver,
    event_ring: EventRing | None,
    stage_id: str,
) -> tuple[MessageView, list[AppliedEffect]]:
    """
    Apply every effect in order, projecting each applied change onto
    `view` so a later stage in the same run sees intended state.

    Args:
        db: Database connection
        view: The message as the run currently sees it
        effects: Effects a stage returned, applied in order
        apply: False in a dry-run sweep -- every effect is resolved and
            recorded as it would have applied, but nothing is written
        folders: Resolves Move's folder reference for this account
        event_ring: Where a Notify effect is emitted, or None to skip it
        stage_id: The stage that produced these effects, for Notify/logs

    Returns:
        (the projected view, one AppliedEffect per input effect)

    Raises:
        StageMisconfigured: a Move's folder reference does not resolve
    """
    applied: list[AppliedEffect] = []
    current = view

    for effect in effects:
        if isinstance(effect, Move):
            target_id = await folders.resolve(
                folder_name=effect.folder_name,
                special_use=effect.special_use,
                folder_id=effect.folder_id,
            )
            if target_id is None:
                raise StageMisconfigured(
                    f"stage {stage_id!r}: move target does not resolve "
                    f"(name={effect.folder_name!r}, special_use={effect.special_use!r}, "
                    f"id={effect.folder_id})",
                    stage_id=stage_id,
                )
            if target_id == current.folder.id:
                applied.append(AppliedEffect(effect, False, "already in target folder"))
                continue
            if not apply:
                applied.append(AppliedEffect(effect, True, f"would move to {target_id}"))
                current = current.with_folder(
                    FolderView(id=target_id, imap_name="(dry-run)", special_use=effect.special_use)
                )
                continue
            rowcount = await _run(db, move_message_guarded,
                current.message_id, target_id, expected_folder_id=current.folder.id)
            if rowcount:
                current = current.with_folder(
                    await _resolve_folder_view(db, target_id, effect.special_use)
                )
                applied.append(AppliedEffect(effect, True, f"moved to {target_id}"))
            else:
                applied.append(AppliedEffect(effect, False, "not applied: message gone"))

        elif isinstance(effect, Trash):
            trash_id = await folders.resolve(special_use="trash")
            if trash_id is None:
                raise StageMisconfigured(
                    f"stage {stage_id!r}: account has no trash folder", stage_id=stage_id,
                )
            if trash_id == current.folder.id:
                applied.append(AppliedEffect(effect, False, "already in trash"))
                continue
            if not apply:
                applied.append(AppliedEffect(effect, True, "would move to trash"))
                current = current.with_folder(
                    FolderView(id=trash_id, imap_name="(dry-run)", special_use="trash")
                )
                continue
            rowcount = await _run(db, move_message_guarded,
                current.message_id, trash_id, expected_folder_id=current.folder.id)
            if rowcount:
                current = current.with_folder(
                    FolderView(id=trash_id, imap_name=current.folder.imap_name, special_use="trash")
                )
                applied.append(AppliedEffect(effect, True, "moved to trash"))
            else:
                applied.append(AppliedEffect(effect, False, "not applied: message gone"))

        elif isinstance(effect, Expunge):
            if not apply:
                applied.append(AppliedEffect(effect, True, "would expunge"))
                continue
            rowcount = await _run(db, expunge_guarded, current.message_id)
            applied.append(
                AppliedEffect(effect, bool(rowcount), "expunged" if rowcount else "already gone")
            )

        elif isinstance(effect, SetFlags):
            flags = {
                k: v for k, v in {
                    "is_seen": effect.seen, "is_flagged": effect.flagged,
                    "is_answered": effect.answered, "is_deleted": effect.deleted,
                }.items() if v is not None
            }
            if not flags:
                applied.append(AppliedEffect(effect, False, "no flags set"))
                continue
            if not apply:
                applied.append(AppliedEffect(effect, True, f"would set {flags}"))
                current = _project_flags(current, flags)
                continue
            rowcount = await _run(db, set_flags_guarded, current.message_id, **flags)
            if rowcount:
                current = _project_flags(current, flags)
                applied.append(AppliedEffect(effect, True, f"set {flags}"))
            else:
                applied.append(AppliedEffect(effect, False, "not applied: message gone"))

        elif isinstance(effect, Keywords):
            if not effect.add and not effect.remove:
                applied.append(AppliedEffect(effect, False, "no keyword change"))
                continue
            new_keywords = tuple(
                kw for kw in (*current.keywords, *effect.add) if kw not in effect.remove
            )
            if not apply:
                applied.append(AppliedEffect(effect, True, f"would set keywords to {new_keywords}"))
                current = current.with_keywords(new_keywords)
                continue
            rowcount = await _run(
                db, set_keywords_delta_guarded, current.message_id,
                add=list(effect.add), remove=list(effect.remove),
            )
            if rowcount:
                current = current.with_keywords(new_keywords)
                applied.append(AppliedEffect(effect, True, f"keywords now {new_keywords}"))
            else:
                applied.append(AppliedEffect(effect, False, "not applied: message gone"))

        elif isinstance(effect, Tag):
            if not apply:
                detail = f"would tag {effect.add}/{effect.remove}"
                applied.append(AppliedEffect(effect, True, detail))
                new_tags = tuple(t for t in (*current.tags, *effect.add) if t not in effect.remove)
                current = current.with_tags(new_tags)
                continue
            await _apply_tags(db, current.message_id, effect)
            new_tags = tuple(t for t in (*current.tags, *effect.add) if t not in effect.remove)
            current = current.with_tags(new_tags)
            applied.append(AppliedEffect(effect, True, f"tags now {new_tags}"))

        elif isinstance(effect, RecordVerdict):
            if not apply:
                applied.append(
                    AppliedEffect(effect, True, f"would record verdict is_spam={effect.is_spam}")
                )
                continue
            recorded = await _record_verdict(db, current, effect)
            applied.append(
                AppliedEffect(
                    effect, recorded,
                    "verdict recorded" if recorded else "already had a verdict for this key",
                )
            )
            if recorded and event_ring is not None:
                await event_ring.add(
                    current.account_id, "verdict.issued",
                    {
                        "message_id": str(current.message_id), "is_spam": effect.is_spam,
                        "source": "ai", "account_id": str(current.account_id),
                    },
                )

        elif isinstance(effect, Notify):
            if event_ring is not None and apply:
                await event_ring.add(
                    current.account_id, "pipeline.notify",
                    {"stage": stage_id, "mail_id": str(current.message_id), "text": effect.text},
                )
            applied.append(AppliedEffect(effect, True, effect.text))

        else:  # pragma: no cover -- exhaustive over the Effect union above
            raise StageMisconfigured(
                f"stage {stage_id!r}: unknown effect {effect!r}", stage_id=stage_id,
            )

    return current, applied


def _project_flags(view: MessageView, flags: dict[str, bool]) -> MessageView:
    # MessageView tracks only is_seen/is_flagged; is_answered and
    # is_deleted have no field to project onto, so they are dropped here
    # -- the write itself still applies via set_flags_guarded regardless.
    changes = {k: v for k, v in flags.items() if k in ("is_seen", "is_flagged")}
    return view.with_flags(**changes) if changes else view


async def _run(
    db: DatabaseConnection,
    func: Callable[..., Awaitable[int]],
    *args: object,
    **kwargs: object,
) -> int:
    async with db.session() as session:
        return await func(session, *args, **kwargs)


async def _resolve_folder_view(
    db: DatabaseConnection, folder_id: uuid.UUID, special_use_hint: str | None,
) -> FolderView:
    from sqlalchemy import select

    from mail_verdict.database.models import Folder

    async with db.session() as session:
        result = await session.execute(
            select(Folder.imap_name, Folder.special_use).where(Folder.id == folder_id)
        )
        row = result.one_or_none()
    if row is None:
        return FolderView(id=folder_id, imap_name="(unknown)", special_use=special_use_hint)
    return FolderView(id=folder_id, imap_name=row.imap_name, special_use=row.special_use)


async def _apply_tags(db: DatabaseConnection, mail_id: uuid.UUID, effect: Tag) -> None:
    async with db.session() as session:
        for tag_name in effect.add:
            stmt = (
                pg_insert(MailTag)
                .values(mail_id=mail_id, tag_name=tag_name, source=TagSource.SPAM)
                .on_conflict_do_nothing(constraint="uq_mail_tag")
            )
            await session.execute(stmt)
        for tag_name in effect.remove:
            await session.execute(
                text("DELETE FROM mail_tags WHERE mail_id = :mail_id AND tag_name = :tag_name"),
                {"mail_id": mail_id, "tag_name": tag_name},
            )


async def _record_verdict(db: DatabaseConnection, view: MessageView, effect: RecordVerdict) -> bool:
    """
    Insert a verdict under ON CONFLICT DO NOTHING against the partial
    unique index on (account_id, msg_key, coalesce(from_addr, '')) WHERE
    source = 'ai' -- this is the never-classify-twice gate holding even
    against two runs racing on the same message, not just the history
    check a stage makes before deciding to classify at all.
    """
    async with db.session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO verdicts
                    (id, mail_id, account_id, message_id_hdr, msg_key, from_addr,
                     is_spam, model_used, reasoning, source)
                VALUES
                    (gen_random_uuid(), :mail_id, :account_id, :message_id_hdr, :msg_key,
                     :from_addr, :is_spam, :model_used, :reasoning, 'ai')
                ON CONFLICT (account_id, msg_key, (coalesce(from_addr, ''))) WHERE (source = 'ai')
                DO NOTHING
                """
            ),
            {
                "mail_id": view.message_id,
                "account_id": view.account_id,
                "message_id_hdr": view.msg_key if not view.msg_key.startswith("sha256:") else None,
                "msg_key": view.msg_key,
                "from_addr": view.from_addr or None,
                "is_spam": effect.is_spam,
                "model_used": effect.model,
                "reasoning": effect.reasoning,
            },
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]
