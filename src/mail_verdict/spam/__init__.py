"""Spam classification support: the classify stage's prompt/schema logic
lives in pipeline/stages/classify.py. What remains here is feedback
recording, its listener, and metrics -- none of it a pipeline stage."""

from mail_verdict.spam.feedback import SpamFeedbackHandler
from mail_verdict.spam.metrics import SpamMetrics
from mail_verdict.spam.processor import SpamFeedbackListener

__all__ = [
    "SpamFeedbackHandler",
    "SpamFeedbackListener",
    "SpamMetrics",
]
