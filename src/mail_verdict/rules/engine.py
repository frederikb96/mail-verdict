"""
Rule evaluation engine.

Loads rules from config, evaluates conditions, runs enrichment,
and executes actions for matching rules. Triggered by PG LISTEN
events for new messages.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from mail_verdict.core.jsonb import parse_jsonb
from mail_verdict.database.models import Attachment, MailTag, Message
from mail_verdict.rules.conditions import MailContext, evaluate_condition
from mail_verdict.rules.enrichment import EnrichmentConfig, EnrichmentResult, EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor, ActionResult, StopProcessing

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# Supported trigger strings from rule config
TRIGGER_TYPES = {
    "mail.received",
    "mail.moved",
    "mail.trashed",
    "mail.deleted",
    "flags.changed",
}


@dataclass
class RuleConfig:
    """Parsed rule from YAML config."""

    name: str
    trigger: str
    enrichment: EnrichmentConfig
    conditions: dict[str, Any]
    actions: list[dict[str, Any]]


@dataclass
class RuleExecutionLog:
    """Log entry for a single rule evaluation."""

    rule_name: str
    triggered: bool
    conditions_matched: bool
    enrichment_result: EnrichmentResult | None = None
    action_results: list[ActionResult] = field(default_factory=list)
    error: str | None = None


def _parse_rules(raw_rules: list[dict[str, Any]]) -> list[RuleConfig]:
    """
    Parse raw rule dicts from YAML config into RuleConfig objects.

    Args:
        raw_rules: List of rule dicts from config.yaml

    Returns:
        Parsed RuleConfig list (preserves config order = priority)
    """
    rules: list[RuleConfig] = []
    for raw in raw_rules:
        name = raw.get("name", "unnamed")
        trigger = raw.get("trigger", "")
        if trigger not in TRIGGER_TYPES:
            logger.warning(
                "Rule has unknown trigger, skipping",
                extra={"rule": name, "trigger": trigger},
            )
            continue

        enrichment_raw = raw.get("enrichment", {})
        enrichment = EnrichmentConfig(
            enabled=enrichment_raw.get("enabled", False),
            prompt=enrichment_raw.get("prompt", ""),
            tags=enrichment_raw.get("tags", []),
        )

        conditions = raw.get("conditions", {})
        # Normalize: list of conditions -> {"all": [...]}
        if isinstance(conditions, list):
            if len(conditions) == 1:
                conditions = conditions[0]
            else:
                conditions = {"all": conditions}

        actions = raw.get("actions", [])

        rules.append(
            RuleConfig(
                name=name,
                trigger=trigger,
                enrichment=enrichment,
                conditions=conditions,
                actions=actions,
            )
        )

    logger.info("Loaded rules from config", extra={"count": len(rules)})
    return rules


# Map PG LISTEN operation types to trigger strings
_OP_TO_TRIGGER: dict[str, str] = {
    "insert": "mail.received",
    "update": "mail.moved",
    "delete": "mail.deleted",
}


class RulesEngine:
    """
    Rule evaluation engine triggered by PG LISTEN events.

    Evaluates rules in config order and executes matching actions.
    Integrates AI enrichment per-rule.
    """

    def __init__(
        self,
        rules: list[dict[str, Any]],
        action_executor: ActionExecutor,
        enrichment_runner: EnrichmentRunner | None = None,
        db: DatabaseConnection | None = None,
    ) -> None:
        """
        Initialize rules engine.

        Args:
            rules: Raw rule dicts from config
            action_executor: Executor for rule actions
            enrichment_runner: Optional AI enrichment runner
            db: Database connection for fetching mail context
        """
        self._rules = _parse_rules(rules)
        self._executor = action_executor
        self._enrichment = enrichment_runner
        self._db = db

    async def handle_message_event(self, event: dict[str, Any]) -> None:
        """
        Handle a message event from PG LISTEN.

        Maps the event operation to a trigger string and evaluates
        all matching rules.

        Args:
            event: PG LISTEN event payload with keys:
                - op: "insert", "update", or "delete"
                - id: Message UUID string
                - account_id: Account UUID string
                - folder_id: Current folder UUID string
                - folder_name: Current folder IMAP name
        """
        op = event.get("op", "")
        trigger_str = _OP_TO_TRIGGER.get(op)
        if not trigger_str:
            return

        matching_rules = [r for r in self._rules if r.trigger == trigger_str]
        if not matching_rules:
            return

        ctx = await self._build_context(event)
        logs: list[RuleExecutionLog] = []

        for rule in matching_rules:
            log = await self._evaluate_rule(rule, ctx, event)
            logs.append(log)
            if log.error == "stop":
                break

        triggered_count = sum(1 for log_entry in logs if log_entry.triggered)
        logger.info(
            "Rules evaluated",
            extra={
                "op": op,
                "rules_checked": len(matching_rules),
                "rules_triggered": triggered_count,
            },
        )

    async def _evaluate_rule(
        self,
        rule: RuleConfig,
        ctx: MailContext,
        event: dict[str, Any],
    ) -> RuleExecutionLog:
        """
        Evaluate a single rule: enrichment -> conditions -> actions.

        Args:
            rule: Rule configuration
            ctx: Mail context
            event: Original PG LISTEN event

        Returns:
            Execution log for this rule
        """
        log = RuleExecutionLog(rule_name=rule.name, triggered=False, conditions_matched=False)

        # Step 1: Run enrichment if enabled
        enrichment_result: EnrichmentResult | None = None
        if rule.enrichment.enabled and self._enrichment:
            enrichment_result = await self._enrichment.run(rule.enrichment, ctx)
            log.enrichment_result = enrichment_result

            if not enrichment_result.success:
                logger.warning(
                    "Enrichment failed, skipping rule",
                    extra={
                        "rule": rule.name,
                        "error": enrichment_result.error,
                    },
                )
                log.error = f"enrichment_failed: {enrichment_result.error}"
                return log

            # Inject enrichment tags into context
            ctx.enrichment_tags = enrichment_result.tags

        # Step 2: Evaluate conditions
        if rule.conditions:
            matched = evaluate_condition(rule.conditions, ctx)
        else:
            matched = True

        log.conditions_matched = matched
        if not matched:
            return log

        # Step 3: Execute actions
        log.triggered = True
        mail_id = ctx.mail_id or self._extract_message_id(event)
        uid = self._extract_imap_uid(event)

        for action in rule.actions:
            try:
                result = await self._executor.execute(
                    action,
                    ctx,
                    mail_id=mail_id,
                    uid=uid,
                )
                log.action_results.append(result)
                if not result.success:
                    logger.warning(
                        "Action failed",
                        extra={
                            "rule": rule.name,
                            "action": result.action_type,
                            "error": result.error,
                        },
                    )
            except StopProcessing:
                log.action_results.append(ActionResult(action_type="stop", success=True))
                log.error = "stop"
                break

        logger.info(
            "Rule executed",
            extra={
                "rule": rule.name,
                "actions_run": len(log.action_results),
                "actions_succeeded": sum(1 for r in log.action_results if r.success),
            },
        )

        return log

    async def _build_context(self, event: dict[str, Any]) -> MailContext:
        """
        Build MailContext from PG LISTEN event and database.

        Args:
            event: The triggering PG LISTEN event

        Returns:
            MailContext populated with message data
        """
        if not self._db:
            return MailContext()

        message_id = self._extract_message_id(event)
        if not message_id:
            return MailContext()

        try:
            async with self._db.session() as session:
                # Fetch message by ID
                result = await session.execute(
                    select(Message).where(Message.id == message_id)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return MailContext()

                # Fetch attachments
                att_result = await session.execute(
                    select(Attachment).where(Attachment.message_id == msg.id)
                )
                attachments = list(att_result.scalars().all())

                # Fetch tags
                tag_result = await session.execute(
                    select(MailTag).where(MailTag.mail_id == msg.id)
                )
                tags = list(tag_result.scalars().all())

                parsed_to = parse_jsonb(msg.to_addrs)
                to_list: list[str] = []
                if parsed_to and isinstance(parsed_to, dict):
                    to_list = parsed_to.get("addrs", [])
                elif parsed_to and isinstance(parsed_to, list):
                    to_list = parsed_to

                parsed_cc = parse_jsonb(msg.cc_addrs)
                cc_list: list[str] = []
                if parsed_cc and isinstance(parsed_cc, dict):
                    cc_list = parsed_cc.get("addrs", [])
                elif parsed_cc and isinstance(parsed_cc, list):
                    cc_list = parsed_cc

                parsed_headers = parse_jsonb(msg.raw_headers)

                folder_name = event.get("folder_name", "")

                return MailContext(
                    mail_id=msg.id,
                    subject=msg.subject or "",
                    body_text=msg.body_text or "",
                    body_html=msg.body_html or "",
                    from_addr=msg.from_addr or "",
                    to_addrs=to_list,
                    cc_addrs=cc_list,
                    raw_headers=parsed_headers if isinstance(parsed_headers, dict) else {},
                    size_bytes=msg.size_bytes or 0,
                    has_attachments=len(attachments) > 0,
                    attachment_types=[a.content_type for a in attachments if a.content_type],
                    folder=folder_name,
                    tags=[t.tag_name for t in tags],
                )

        except Exception as exc:
            logger.error(
                "Failed to build mail context from database",
                extra={"error": str(exc)},
            )
            return MailContext()

    def _extract_message_id(self, event: dict[str, Any]) -> uuid.UUID | None:
        """Extract message UUID from PG LISTEN event."""
        id_str = event.get("id")
        if not id_str:
            return None
        try:
            return uuid.UUID(id_str)
        except (ValueError, TypeError):
            return None

    def _extract_imap_uid(self, event: dict[str, Any]) -> int:
        """Extract IMAP UID from PG LISTEN event if available."""
        return int(event.get("imap_uid", 0))
