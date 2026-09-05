"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, retry, pipeline, semantic, calendar.

"rules" is not one of them: a rule is a `match` stage in the pipeline
now (see pipeline/stages/match.py), and `settings.rules` -- if a pre-
existing deployment still has one -- is read exactly once, by the
migration that builds the first pipeline revision from it (see
alembic/versions/0006_pipeline.py), never at runtime after that.

"spam" is not one of them either, for the same reason: whether spam
detection runs at all is account_prefs.spam_enabled (a per-account
preference, PATCH /api/accounts/{id}), and auto-move/auto-mark-read are
pipeline stages an account's own pipeline document configures --
GET/PUT /api/settings/spam would read and write a category nothing
outside that same migration ever looks at again.

"""

from __future__ import annotations

import enum
from typing import Any


class SettingCategory(str, enum.Enum):
    """Valid setting categories."""

    AI = "ai"
    RETRY = "retry"
    PIPELINE = "pipeline"
    SEMANTIC = "semantic"
    CALENDAR = "calendar"


SETTING_DEFAULTS: dict[str, dict[str, Any]] = {
    SettingCategory.AI: {
        # "openai" and "anthropic" both need their provider's API key
        # configured (settings/credentials.py, or the matching env var).
        # "fake" classifies on keywords alone, for local use without a key.
        "provider": "openai",
        "model": "gpt-5.4-nano",
        # "none" matches gpt-5.4-nano's own server-side default. Raising
        # this is a per-model gamble, not a guaranteed quality lever: a
        # lightweight classification task may not spend any reasoning
        # tokens at higher effort either, so measure before assuming a
        # higher setting changes anything for a given model.
        "reasoning_effort": "none",
        "max_tokens": 1024,
    },
    SettingCategory.RETRY: {
        "max_retries": 5,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 20.0,
        "exponential_base": 2.0,
    },
    SettingCategory.PIPELINE: {
        # Worker claim/lease mechanics -- see queue/work_queue.py.
        "lease_seconds": 120,
        "poll_interval_seconds": 2.0,
        # Retry backoff for a stage raising StageTransient or an unmapped
        # exception; full jitter, see queue/backoff.py.
        "max_attempts": 5,
        "base_delay_seconds": 2.0,
        "max_delay_seconds": 60.0,
        # How long a suspended or throttled run waits before becoming
        # claimable again -- distinct from the circuit breaker's own
        # probe interval, which gates whether a call is attempted at all.
        "unavailable_probe_seconds": 60,
        # Reconciliation's secondary guard against a missing or stale
        # per-folder watermark: a message older than this is never
        # treated as live-eligible, however its folder's watermark reads.
        "live_max_age_days": 7,
    },
    SettingCategory.SEMANTIC: {
        # "openai" is the only real provider -- Anthropic has no embedding
        # model of its own to select here, unlike ai.provider. "fake"
        # produces deterministic hash-derived vectors for local use
        # without a key.
        "provider": "openai",
        "model": "text-embedding-3-small",
        # Gates the periodic reconciler that enqueues missing embeddings
        # (embeddings/worker.py) -- search and the manual backfill endpoint
        # still work with this off, they just find nothing new to fill.
        "enabled": True,
        # Every model is asked to truncate to EMBEDDING_DIMENSIONS
        # (database/models.py) via its own dimensions parameter, so this
        # is never a setting -- changing the column width is a migration.
        "content_chars": 2000,
        # How many missing-embedding candidates the backfill reconciler
        # considers per sweep tick (embeddings/worker.py's _reconcile) --
        # not a queue claim size. The worker itself always claims one row
        # at a time; see the "embeddings" queue's concurrency instead
        # (queue_state, changed through the queue API) for how many of
        # those run in parallel.
        "batch_size": 64,
        # Neighbour hints in the classify stage's prompt: the k nearest
        # past messages carrying a human label (a user correction, or the
        # folder they currently sit in -- never the classifier's own past
        # verdicts, see pipeline/neighbors.py). Off by default so the
        # effect can be measured against spam/metrics.py before it is
        # ever the default; a low similarity floor keeps a weak match
        # from padding the prompt with noise.
        "neighbor_hints_enabled": False,
        "neighbor_k": 5,
        "neighbor_min_similarity": 0.75,
        # The semantic search endpoint's own fallback when a caller sends
        # no strictness of its own (the search page always sends one, its
        # own localStorage-persisted preference -- see search-prefs.ts;
        # this only matters for another caller, e.g. the MCP tool). One
        # of "loose"/"balanced"/"strict" -- see embeddings/search.py for
        # what each resolves to.
        "default_strictness": "balanced",
        # Retry backoff for a retryable provider error that is specific to
        # one payload (a connection drop, a 5xx, a timeout) rather than a
        # shared-resource throttle -- full jitter, see queue/backoff.py. A
        # rate limit is never capped by this: that is provider-wide, not
        # the item's fault, and release_untouched leaves it retryable
        # forever, the same way an unconfigured key is.
        "max_attempts": 5,
        "base_delay_seconds": 2.0,
        "max_delay_seconds": 60.0,
    },
    SettingCategory.CALENDAR: {
        # A click on empty grid space creates an event this long; a drag
        # also snaps to boundaries this many minutes apart. One value
        # serves both, the same way the grid's own snap constant always
        # has -- a shorter snap than the default duration would let a
        # drag land on a boundary the click-created default never uses.
        "default_event_duration_minutes": 30,
        # The calendar a new event's editor opens on when nothing more
        # specific names one -- an id from dav_collections, unenforced by
        # a foreign key the same way every other reference onto a
        # PostIMAP-owned table is. None until a person picks one; the
        # event editor's own fallback (the first writable calendar) is
        # what a client uses meanwhile.
        "default_calendar_id": None,
    },
}
