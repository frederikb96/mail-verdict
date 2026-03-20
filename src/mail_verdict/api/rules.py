"""
Rule API endpoints.

GET /api/rules — list configured rules
POST /api/rules/:id/test — test a rule against a specified mail (dry-run)

Rules are stored in the settings DB under category 'rules'.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from mail_verdict.api.schemas import RuleResponse, RuleTestRequest, RuleTestResponse
from mail_verdict.settings import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rules", tags=["rules"])


def _get_rules_list() -> list[dict[str, Any]]:
    """Load the rules list from settings."""
    service = get_settings_service()
    try:
        rules_data = service.get("rules")
    except Exception:
        return []
    if isinstance(rules_data, dict):
        result = rules_data.get("rules", [])
        return result if isinstance(result, list) else []
    return []


@router.get("", response_model=list[RuleResponse])
async def list_rules() -> list[RuleResponse]:
    """List all configured rules."""
    raw_rules = _get_rules_list()
    rules: list[RuleResponse] = []
    for i, raw in enumerate(raw_rules):
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
    raw_rules = _get_rules_list()
    if rule_index < 0 or rule_index >= len(raw_rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    raw = raw_rules[rule_index]
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
    raw_rules = _get_rules_list()
    if rule_index < 0 or rule_index >= len(raw_rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    raw = raw_rules[rule_index]
    rule_name = raw.get("name", "unnamed")
    conditions = raw.get("conditions", {})
    actions = raw.get("actions", [])

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
