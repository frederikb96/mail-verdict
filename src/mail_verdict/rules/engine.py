"""
Rule evaluation engine.

Loads rules from config, subscribes to the event bus, evaluates conditions,
runs enrichment, and executes actions for matching rules.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.rules.conditions import MailContext, evaluate_condition
from mail_verdict.rules.enrichment import EnrichmentConfig, EnrichmentResult, EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor, ActionResult, StopProcessing
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailMoved,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# Map trigger strings from config to SyncEvent types
TRIGGER_MAP: dict[str, type[SyncEvent]] = {
    "mail.received": MailReceived,
    "mail.moved": MailMoved,
    "mail.trashed": MailTrashed,
    "mail.spam_detected": MailSpamDetected,
    "mail.deleted": MailDeleted,
    "flags.changed": FlagsChanged,
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
        if trigger not in TRIGGER_MAP:
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


class RulesEngine:
    """
    Event-driven rule evaluation engine.

    Subscribes to the event bus, evaluates rules in config order,
    and executes matching actions. Integrates AI enrichment per-rule.
    """

    def __init__(
        self,
        rules: list[dict[str, Any]],
        bus: EventBus,
        action_executor: ActionExecutor,
        enrichment_runner: EnrichmentRunner | None = None,
        db: DatabaseConnection | None = None,
    ) -> None:
        """
        Initialize rules engine.

        Args:
            rules: Raw rule dicts from config
            bus: Event bus to subscribe to
            action_executor: Executor for rule actions
            enrichment_runner: Optional AI enrichment runner
            db: Database connection for fetching mail context
        """
        self._rules = _parse_rules(rules)
        self._bus = bus
        self._executor = action_executor
        self._enrichment = enrichment_runner
        self._db = db

    async def start(self) -> None:
        """Register with the event bus for all trigger types used by rules."""
        trigger_types: set[type[SyncEvent]] = set()
        for rule in self._rules:
            event_type = TRIGGER_MAP.get(rule.trigger)
            if event_type:
                trigger_types.add(event_type)

        for event_type in trigger_types:
            await self._bus.subscribe(
                event_type,
                Subscriber(
                    name="rules_engine",
                    callback=self._handle_event,
                    priority=50,
                ),
            )

        logger.info(
            "Rules engine started",
            extra={
                "rule_count": len(self._rules),
                "trigger_types": [t.__name__ for t in trigger_types],
            },
        )

    async def stop(self) -> None:
        """Unsubscribe from the event bus."""
        for event_type in TRIGGER_MAP.values():
            await self._bus.unsubscribe(event_type, "rules_engine")
        logger.info("Rules engine stopped")

    async def _handle_event(self, event: SyncEvent) -> None:
        """
        Handle a dispatched event by evaluating matching rules.

        Args:
            event: The SyncEvent to process
        """
        event_name = type(event).__name__
        trigger_str = self._event_to_trigger(event)
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
                "event": event_name,
                "rules_checked": len(matching_rules),
                "rules_triggered": triggered_count,
            },
        )

    async def _evaluate_rule(
        self,
        rule: RuleConfig,
        ctx: MailContext,
        event: SyncEvent,
    ) -> RuleExecutionLog:
        """
        Evaluate a single rule: enrichment -> conditions -> actions.

        Args:
            rule: Rule configuration
            ctx: Mail context
            event: Original event

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
        mail_id = self._extract_mail_id(event)
        uid = self._extract_uid(event)

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

    async def _build_context(self, event: SyncEvent) -> MailContext:
        """
        Build MailContext from event and database.

        Args:
            event: The triggering event

        Returns:
            MailContext populated with mail data
        """
        if not self._db:
            return MailContext()

        uid = self._extract_uid(event)
        if uid == 0:
            return MailContext()

        try:
            from sqlalchemy import select

            from mail_verdict.database.models import Attachment, Mail, MailTag

            async with self._db.session() as session:
                # Fetch mail by folder_id + uid
                result = await session.execute(
                    select(Mail).where(
                        Mail.folder_id == event.folder_id,
                        Mail.uid == uid,
                    )
                )
                mail = result.scalar_one_or_none()
                if not mail:
                    return MailContext()

                # Fetch attachments
                att_result = await session.execute(
                    select(Attachment).where(Attachment.mail_id == mail.id)
                )
                attachments = list(att_result.scalars().all())

                # Fetch tags
                tag_result = await session.execute(
                    select(MailTag).where(MailTag.mail_id == mail.id)
                )
                tags = list(tag_result.scalars().all())

                to_list: list[str] = []
                if mail.to_addrs and isinstance(mail.to_addrs, dict):
                    to_list = mail.to_addrs.get("addrs", [])
                elif mail.to_addrs and isinstance(mail.to_addrs, list):
                    to_list = mail.to_addrs

                cc_list: list[str] = []
                if mail.cc_addrs and isinstance(mail.cc_addrs, dict):
                    cc_list = mail.cc_addrs.get("addrs", [])
                elif mail.cc_addrs and isinstance(mail.cc_addrs, list):
                    cc_list = mail.cc_addrs

                return MailContext(
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
                    folder=self._get_folder_name(event),
                    tags=[t.tag_name for t in tags],
                )

        except Exception as exc:
            logger.error(
                "Failed to build mail context from database",
                extra={"error": str(exc)},
            )
            return MailContext()

    def _event_to_trigger(self, event: SyncEvent) -> str | None:
        """Map a SyncEvent instance to its trigger string."""
        for trigger, event_type in TRIGGER_MAP.items():
            if isinstance(event, event_type):
                return trigger
        return None

    def _extract_uid(self, event: SyncEvent) -> int:
        """Extract UID from event if available."""
        return getattr(event, "uid", 0)

    def _extract_mail_id(self, event: SyncEvent) -> uuid.UUID | None:
        """Extract mail_id from event if available."""
        return getattr(event, "mail_id", None)

    def _get_folder_name(self, event: SyncEvent) -> str:
        """Get folder name from event context."""
        return getattr(event, "folder_name", "")
