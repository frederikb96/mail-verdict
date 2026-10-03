"""
Sending one webhook's deliveries for mail that already exists.

Rules act on arrivals only, so mail that predates a rule is never sent by
it. This is the supported way to send it: the named webhook's rule
conditions are evaluated against the mail received since a date, and a
delivery is queued for every match, oldest first.

Safe to repeat. A delivery row blocks the same (webhook, mail) from being
queued again whatever its status, so a second call queues only what the
first did not reach -- mail that arrived since, or mail whose rule now
matches. The queued rows carry a lower priority than live mail and are
spaced by their received order, which the single delivery worker then
sends one after another; a delivery that has to be retried is retried
later and does not hold up the ones behind it.

Only the live mirror is scanned: mail already moved to a glacier is not a
candidate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import exists, select

from mail_verdict.database.models import Message, WebhookDelivery
from mail_verdict.pipeline.context import current_verdict_for_mail
from mail_verdict.pipeline.contracts import StageDefinition, Webhook
from mail_verdict.pipeline.effect_codec import parse_effect
from mail_verdict.pipeline.message_view import load_message_view
from mail_verdict.pipeline.runner import _SKIP_FOLDER_SPECIAL_USE
from mail_verdict.pipeline.stages.match import MatchConfig, matches_message
from mail_verdict.webhooks.repository import enqueue_delivery

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

# Behind live mail (priority 0) in the single worker's claim order.
BACKFILL_PRIORITY = 100
# Keeps the queued rows' due times strictly ordered by received time.
_SPACING = timedelta(milliseconds=1)


class WebhookNotConfiguredError(LookupError):
    """No enabled `match` stage carries a webhook action of that name."""


@dataclass(frozen=True)
class BackfillResult:
    """What a backfill found and did. `queued` counts rows inserted (or that
    would be, in a dry run); `already_queued` are matches that already had a
    delivery of any status. `truncated` is true when `limit` stopped the
    scan, so a further call continues where it left off."""

    scanned: int
    matched: int
    queued: int
    already_queued: int
    truncated: bool


def find_webhook(
    stages: tuple[StageDefinition, ...], name: str,
) -> tuple[StageDefinition, Webhook]:
    """The enabled `match` stage and webhook effect carrying `name`.

    Raises:
        WebhookNotConfiguredError: there is none
    """
    for stage in stages:
        if stage.type != "match" or not stage.enabled:
            continue
        config = MatchConfig.model_validate(dict(stage.config))
        for raw in config.effects:
            effect = parse_effect(raw)
            if isinstance(effect, Webhook) and effect.name == name:
                return stage, effect
    raise WebhookNotConfiguredError(f"no enabled rule has a webhook action named {name!r}")


async def backfill_webhook(
    db: DatabaseConnection, stages: tuple[StageDefinition, ...], name: str, *,
    since: datetime, until: datetime | None, limit: int, dry_run: bool,
) -> BackfillResult:
    """
    Queue the named webhook's deliveries for existing matching mail.

    Args:
        db: Database connection
        stages: The current pipeline definition's stages
        name: The webhook action's name
        since: Only mail received at or after this
        until: Only mail received before this, or None for no upper bound
        limit: Most candidate messages to look at in this call
        dry_run: Count what would be queued without queueing it

    Raises:
        WebhookNotConfiguredError: no enabled rule carries that webhook
    """
    stage, effect = find_webhook(stages, name)
    when = MatchConfig.model_validate(dict(stage.config)).when

    stmt = (
        select(Message.id)
        .where(
            Message.received_at >= since, Message.expunged_at.is_(None),
            Message.is_draft.is_(False),
        )
        .order_by(Message.received_at, Message.id)
        .limit(limit)
    )
    if until is not None:
        stmt = stmt.where(Message.received_at < until)
    if stage.accounts:
        stmt = stmt.where(Message.account_id.in_(list(stage.accounts)))

    async with db.session() as session:
        candidate_ids: list[uuid.UUID] = list((await session.execute(stmt)).scalars().all())

    scanned = matched = queued = already = 0
    base = datetime.now(timezone.utc)
    for message_id in candidate_ids:
        scanned += 1
        async with db.session() as session:
            view = await load_message_view(session, message_id)
        if view is None or view.is_draft:
            continue
        if (view.folder.special_use or "") in _SKIP_FOLDER_SPECIAL_USE:
            continue
        verdict = await current_verdict_for_mail(db, message_id)
        if not matches_message(when, view, verdict):
            continue
        matched += 1
        if dry_run:
            # Without a write, whether a row exists is still the question.
            async with db.session() as session:
                present = (
                    await session.execute(
                        select(exists().where(
                            WebhookDelivery.name == name,
                            WebhookDelivery.account_id == view.account_id,
                            WebhookDelivery.msg_key == view.msg_key,
                        ))
                    )
                ).scalar_one()
            already += int(present)
            queued += int(not present)
            continue
        async with db.session() as session:
            inserted = await enqueue_delivery(
                session, effect, account_id=view.account_id, msg_key=view.msg_key,
                message_id=message_id, origin="backfill", priority=BACKFILL_PRIORITY,
                next_attempt_at=base + _SPACING * matched,
            )
        if inserted:
            queued += 1
        else:
            already += 1
    return BackfillResult(
        scanned=scanned, matched=matched, queued=queued, already_queued=already,
        truncated=len(candidate_ids) >= limit,
    )
