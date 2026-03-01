"""MailVerdict IMAP sync engine."""

from mail_verdict.sync.actions import ActionPropagator, ActionType, ForwardAction, IMAPAction
from mail_verdict.sync.change_detector import ChangeDetector, ChangeSet
from mail_verdict.sync.connector import IMAPConnectionError, IMAPConnector
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
from mail_verdict.sync.extensions import AsyncIMAPExtended, FolderInfo, SelectResult
from mail_verdict.sync.folders import detect_special_use, discover_folders
from mail_verdict.sync.idle import IdleWatcher
from mail_verdict.sync.manager import SyncManager
from mail_verdict.sync.parser import AuthResult, ParsedMail, parse_message
from mail_verdict.sync.smtp_client import SMTPClient, SMTPError

__all__ = [
    "ActionPropagator",
    "ActionType",
    "AccountSync",
    "AsyncIMAPExtended",
    "AuthResult",
    "ChangeDetector",
    "ChangeSet",
    "FlagsChanged",
    "FolderInfo",
    "ForwardAction",
    "IMAPAction",
    "IMAPConnectionError",
    "IMAPConnector",
    "IdleWatcher",
    "MailDeleted",
    "MailMoved",
    "MailReceived",
    "MailSpamDetected",
    "MailTrashed",
    "ParsedMail",
    "SMTPClient",
    "SMTPError",
    "SelectResult",
    "SyncEngine",
    "SyncEvent",
    "SyncManager",
    "detect_special_use",
    "discover_folders",
    "parse_message",
]
