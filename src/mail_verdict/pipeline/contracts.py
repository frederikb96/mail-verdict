"""
The stage contract: what a stage receives, what it returns, and how it
signals failure.

A stage never writes SQL and never touches a database session -- it reads
a MessageView and a RunContext and returns a StageOutcome carrying
declarative Effects, which is what makes a stage dry-runnable and
unit-testable with nothing running. A stage that cannot do its job raises
one of the exceptions below rather than returning a success flag: a
result type with a success field is exactly how a rule that silently did
nothing gets recorded as having worked.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from pydantic import BaseModel

    from mail_verdict.pipeline.context import RunContext
    from mail_verdict.pipeline.message_view import MessageView

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


# --- Effects -----------------------------------------------------------
#
# Closed and small on purpose: each one maps to exactly one call in
# postimap/actions.py or one write to a MailVerdict-owned table. Adding a
# new kind of change means adding a new Effect variant here, never giving
# a stage a way to write SQL directly.


@dataclass(frozen=True)
class Move:
    """Move the message to another folder, identified by name, special_use
    or id -- resolved against the run's account by FolderResolver."""

    folder_name: str | None = None
    special_use: str | None = None
    folder_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Trash:
    """Move the message to the account's trash folder."""


@dataclass(frozen=True)
class Expunge:
    """Permanently remove the message."""


@dataclass(frozen=True)
class SetFlags:
    """Set one or more IMAP-mapped flags. Unset fields are left alone."""

    seen: bool | None = None
    flagged: bool | None = None
    answered: bool | None = None
    deleted: bool | None = None


@dataclass(frozen=True)
class Keywords:
    """Add and/or remove custom IMAP keywords -- a delta, never a
    replacement, so two effects touching keywords in the same run (or a
    concurrent write from PostIMAP) can never lose each other's change."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tag:
    """Add and/or remove MailVerdict tags."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordVerdict:
    """Write a spam/ham verdict. Applied with ON CONFLICT DO NOTHING under
    the durability index, so a duplicate run re-recording the same verdict
    is a no-op rather than an error."""

    is_spam: bool
    reasoning: str
    model: str | None = None


@dataclass(frozen=True)
class Notify:
    """Emit a one-line observability event; never seen by a mailbox UI."""

    text: str


Effect = Move | Trash | Expunge | SetFlags | Keywords | Tag | RecordVerdict | Notify


@dataclass(frozen=True)
class Usage:
    """Model-call accounting for one stage's execution."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


@dataclass(frozen=True)
class StageOutcome:
    """What a stage did with one message. `matched=False, effects=()` is
    the ordinary and cheap "this stage did not apply" case."""

    matched: bool
    effects: tuple[Effect, ...] = ()
    halt: bool = False
    detail: str | None = None
    facts: Mapping[str, JsonValue] = field(default_factory=dict)
    usage: Usage | None = None


# --- Failure -------------------------------------------------------------


class StageError(Exception):
    """Base for every exception a stage may raise. Never raised directly."""


class StageTransient(StageError):
    """A provider 5xx, a timeout, a deadlock -- retry the run with backoff."""


class StageThrottled(StageError):
    """A 429. The attempt is refunded and the whole pool backs off rather
    than only this run."""

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class StageUnavailable(StageError):
    """Auth rejected outright, or no key configured. Suspends the queue;
    the attempt is refunded, since the item itself did nothing wrong."""


class StageMisconfigured(StageError):
    """A folder that does not resolve, an unknown stage type -- something a
    person has to fix. Deliberately not retried: retrying it for a week
    produces noise and hides the problem instead of surfacing it."""

    def __init__(self, message: str, *, stage_id: str | None = None) -> None:
        super().__init__(message)
        self.stage_id = stage_id


# --- The stage protocol ---------------------------------------------------


class Stage(Protocol):
    """One configured unit in the pipeline."""

    type: ClassVar[str]
    runs_on: ClassVar[frozenset[str]]

    @classmethod
    def config_schema(cls) -> builtins.type[BaseModel]:
        """The Pydantic model this type's `config` must validate against."""
        ...

    def __init__(self, stage_id: str, config: BaseModel) -> None: ...

    async def execute(self, msg: MessageView, ctx: RunContext) -> StageOutcome:
        """
        Apply this stage to one message.

        Must be idempotent: a crash mid-run re-executes from the first
        stage, so running twice against the same message must produce the
        same effects, not double them.

        Raises:
            StageTransient | StageThrottled | StageUnavailable |
            StageMisconfigured: on any failure. Never returns a success
                flag -- see the module docstring.
        """
        ...


@dataclass(frozen=True)
class StageDefinition:
    """One entry in a pipeline revision's stage list."""

    stage_id: str
    type: str
    name: str
    config: Mapping[str, JsonValue]
    enabled: bool = True
    halt: bool = False
    accounts: Sequence[uuid.UUID] | None = None
