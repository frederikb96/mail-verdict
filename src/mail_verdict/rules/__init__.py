"""MailVerdict rules engine: event-driven, config-based rule evaluation."""

from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.rules.conditions import ConditionEvaluator, evaluate_condition
from mail_verdict.rules.engine import RulesEngine
from mail_verdict.rules.enrichment import EnrichmentResult, EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor
from mail_verdict.rules.tags import TagSyncService

__all__ = [
    "ActionExecutor",
    "ConditionEvaluator",
    "EnrichmentResult",
    "EnrichmentRunner",
    "EventBus",
    "RulesEngine",
    "Subscriber",
    "TagSyncService",
    "evaluate_condition",
]
