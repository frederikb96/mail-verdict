"""Background job management for MailVerdict."""

from mail_verdict.jobs.base import Job, JobStatus
from mail_verdict.jobs.manager import JobManager, get_job_manager

__all__ = [
    "Job",
    "JobManager",
    "JobStatus",
    "get_job_manager",
]
