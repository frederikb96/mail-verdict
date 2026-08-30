"""Condition evaluation shared by the pipeline's `match` stage.

The rule engine itself is retired -- a rule is a `match` stage in the
pipeline (see pipeline/stages/match.py); the condition tree it evaluates
still lives here, unchanged in shape, plus `verdict_is`.
"""

from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.rules.conditions import ConditionEvaluator, evaluate_condition
from mail_verdict.rules.tags import TagSyncService

__all__ = [
    "ConditionEvaluator",
    "EventBus",
    "Subscriber",
    "TagSyncService",
    "evaluate_condition",
]
