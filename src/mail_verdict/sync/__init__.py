"""MailVerdict IMAP sync engine."""

from mail_verdict.sync.actions import (
    ActionPropagator,
    ActionType,
    BatchResult,
    ForwardAction,
    IMAPAction,
)
from mail_verdict.sync.change_detector import ChangeDetector, ChangeSet
from mail_verdict.sync.connector import IMAPConnectionError, IMAPConnector, is_connection_error
from mail_verdict.sync.engine import AccountSync, SyncEngine
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailMoved,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)
from mail_verdict.sync.folders import FolderInfo, detect_special_use, discover_folders
from mail_verdict.sync.idle import IdleWatcher
from mail_verdict.sync.manager import SyncManager
from mail_verdict.sync.parser import AuthResult, ParsedMail, parse_message
from mail_verdict.sync.smtp_client import SMTPClient, SMTPError
from mail_verdict.sync.tracker import SyncPhase, SyncTracker

__all__ = [
    "ActionPropagator",
    "ActionType",
    "AccountSync",
    "BatchResult",
    "AuthResult",
    "ChangeDetector",
    "ChangeSet",
    "FlagsChanged",
    "FolderInfo",
    "ForwardAction",
    "IMAPAction",
    "IMAPConnectionError",
    "IMAPConnector",
    "is_connection_error",
    "IdleWatcher",
    "MailDeleted",
    "MailMoved",
    "MailReceived",
    "MailSpamDetected",
    "MailTrashed",
    "ParsedMail",
    "SMTPClient",
    "SMTPError",
    "SyncEngine",
    "SyncEvent",
    "SyncManager",
    "SyncPhase",
    "SyncTracker",
    "detect_special_use",
    "discover_folders",
    "parse_message",
]
