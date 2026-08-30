"""MailVerdict spam detection module."""

from mail_verdict.spam.analyst import (
    AnthropicSpamAnalyst,
    FakeSpamAnalyst,
    SpamAnalyst,
    SpamVerdict,
)
from mail_verdict.spam.feedback import SpamFeedbackHandler
from mail_verdict.spam.metrics import SpamMetrics
from mail_verdict.spam.pipeline import VerdictPipeline
from mail_verdict.spam.processor import SpamEventProcessor

__all__ = [
    "AnthropicSpamAnalyst",
    "FakeSpamAnalyst",
    "SpamAnalyst",
    "SpamEventProcessor",
    "SpamFeedbackHandler",
    "SpamMetrics",
    "SpamVerdict",
    "VerdictPipeline",
]
