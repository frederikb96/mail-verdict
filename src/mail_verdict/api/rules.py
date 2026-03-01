"""
Rule API endpoints.

GET /api/rules — list configured rules
POST /api/rules/:id/test — test a rule against a specified mail (dry-run)

Rules are defined in config.yaml, so create/update/delete are
not applicable at runtime (config-driven). The API exposes read
and test capabilities.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from mail_verdict.api.schemas import RuleResponse, RuleTestRequest, RuleTestResponse
from mail_verdict.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("", response_model=list[RuleResponse])
async def list_rules() -> list[RuleResponse]:
    """List all configured rules."""
    config = get_config()
    rules: list[RuleResponse] = []
    for i, raw in enumerate(config.rules):
        rules.append(
            RuleResponse(
                index=i,
                name=raw.get("name", "unnamed"),
                trigger=raw.get("trigger", ""),
                conditions=raw.get("conditions", {}),
                actions=raw.get("actions", []),
                enrichment=raw.get("enrichment", {}),
            )
        )
    return rules


@router.get("/{rule_index}", response_model=RuleResponse)
async def get_rule(rule_index: int) -> RuleResponse:
    """Get a single rule by its config index."""
    config = get_config()
    if rule_index < 0 or rule_index >= len(config.rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    raw = config.rules[rule_index]
    return RuleResponse(
        index=rule_index,
        name=raw.get("name", "unnamed"),
        trigger=raw.get("trigger", ""),
        conditions=raw.get("conditions", {}),
        actions=raw.get("actions", []),
        enrichment=raw.get("enrichment", {}),
    )


@router.post("/{rule_index}/test", response_model=RuleTestResponse)
async def test_rule(
    rule_index: int,
    request: RuleTestRequest,
) -> RuleTestResponse:
    """
    Test a rule against a specified mail in dry-run mode.

    Evaluates conditions but does not execute actions.
    """
    config = get_config()
    if rule_index < 0 or rule_index >= len(config.rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    raw = config.rules[rule_index]
    rule_name = raw.get("name", "unnamed")
    conditions = raw.get("conditions", {})
    actions = raw.get("actions", [])

    # Build context from mail
    from sqlalchemy import select

    from mail_verdict.database.connection import get_db_connection
    from mail_verdict.database.models import Attachment, Mail, MailTag
    from mail_verdict.rules.conditions import MailContext, evaluate_condition

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Mail).where(
                Mail.id == request.mail_id,
                Mail.account_id == request.account_id,
            )
        )
        mail = result.scalar_one_or_none()
        if mail is None:
            raise HTTPException(status_code=404, detail="Mail not found")

        att_result = await session.execute(select(Attachment).where(Attachment.mail_id == mail.id))
        attachments = list(att_result.scalars().all())

        tag_result = await session.execute(select(MailTag).where(MailTag.mail_id == mail.id))
        tags = list(tag_result.scalars().all())

    to_list = _extract_addr_list(mail.to_addrs)
    cc_list = _extract_addr_list(mail.cc_addrs)

    ctx = MailContext(
        subject=mail.subject or "",
        body_text=mail.body_text or "",
        body_html=mail.body_html or "",
        from_addr=mail.from_addr or "",
        to_addrs=to_list,
        cc_addrs=cc_list,
        raw_headers=mail.raw_headers or {},
        size_bytes=mail.size_bytes or 0,
        has_attachments=len(attachments) > 0,
        attachment_types=[a.content_type for a in attachments if a.content_type],
        tags=[t.tag_name for t in tags],
    )

    # Normalize conditions (same as RulesEngine)
    if isinstance(conditions, list):
        if len(conditions) == 1:
            conditions = conditions[0]
        else:
            conditions = {"all": conditions}

    matched = evaluate_condition(conditions, ctx) if conditions else True

    return RuleTestResponse(
        rule_name=rule_name,
        conditions_matched=matched,
        actions_would_run=actions if matched else [],
    )


def _extract_addr_list(addrs: Any) -> list[str]:
    """Extract a flat list of addresses from the JSONB field."""
    if isinstance(addrs, dict):
        return list(addrs.values())
    if isinstance(addrs, list):
        return addrs
    return []
