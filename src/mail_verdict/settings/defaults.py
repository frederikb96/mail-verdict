"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, retry, pipeline, semantic, calendar, outbox, mail.

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
    OUTBOX = "outbox"
    MAIL = "mail"
    ORDERS = "orders"


SETTING_DEFAULTS: dict[str, dict[str, Any]] = {
    SettingCategory.AI: {
        # "openai" and "anthropic" both need their provider's API key
        # configured (settings/credentials.py, or the matching env var).
        # "custom" is any OpenAI-compatible chat-completions server reached
        # at ai.base_url with its own key (settings/credentials.py's
        # "custom" entry) -- the same slot semantic.provider == "custom"
        # reads, since a custom deployment is one account serving both
        # workloads. "fake" classifies on keywords alone, for local use
        # without a key.
        "provider": "openai",
        "model": "gpt-5.4-nano",
        # "none" matches gpt-5.4-nano's own server-side default. Raising
        # this is a per-model gamble, not a guaranteed quality lever: a
        # lightweight classification task may not spend any reasoning
        # tokens at higher effort either, so measure before assuming a
        # higher setting changes anything for a given model.
        "reasoning_effort": "none",
        "max_tokens": 1024,
        # Read only when provider == "custom" -- the compatible server's
        # API base, e.g. "https://api.infomaniak.com/2/ai/<product_id>/
        # openai/v1". Ignored for "openai"/"anthropic"/"fake", and required
        # by settings/ai_validation.py whenever provider is "custom".
        "base_url": None,
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
        # "openai" or "custom" (any OpenAI-compatible embeddings endpoint,
        # reached at semantic.base_url) -- Anthropic has no embedding model
        # of its own to select here, unlike ai.provider. "fake" produces
        # deterministic hash-derived vectors for local use without a key.
        "provider": "openai",
        "model": "text-embedding-3-small",
        # Read only when provider == "custom" -- see ai.base_url's comment;
        # the two are independent settings, so pointing both categories at
        # the same compatible server means setting this the same way there.
        "base_url": None,
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
        # verdicts, see pipeline/neighbors.py). Always on -- a low
        # similarity floor keeps a weak match from padding the prompt
        # with noise.
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
        # The model actually serving search and the classify stage's
        # neighbour hints right now -- distinct from `model`, which is the
        # target new mail is embedded with and the backfill reconciler
        # fills toward. None means "whatever `model` currently is" (the
        # steady state, nothing mid-migration). Changing `model` freezes
        # this to the model it resolved to just before the change
        # (api/settings_api.py's update_settings), so search keeps
        # answering from the old vector space until every in-scope message
        # has a `model` embedding (embeddings/worker.py's reconciler,
        # checked via embeddings/repository.py's coverage), at which point
        # the reconciler advances this to match and the cutover is
        # complete. Frozen and advanced together with `model` --
        # embeddings/provider.py's resolve_active_embedding_model()/
        # resolve_active_embedding_provider() are the two places that read
        # them, for embedding a fresh search query against whichever
        # vector space is actually complete.
        "active_model": None,
        "active_provider": None,
        "active_base_url": None,
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
        # Minutes before an event's start a freshly created event defaults
        # to reminding at, on a calendar with no override of its own --
        # calendar_prefs.default_reminder_minutes/reminders_enabled, and
        # calendar/prefs.py's resolve_default_reminder() resolves the two
        # together. Applied only when the editor's create form opens,
        # never implicitly on the server at save time, which would put
        # alarms on events the MCP tools or invitation intake create.
        "default_reminder_minutes": 15,
    },
    SettingCategory.OUTBOX: {
        # How long a send sits in MailVerdict's own staging table before
        # the periodic worker (outbox/pending.py) turns it into a real
        # outbox insert -- the window "undo send" cancels within. 0 skips
        # staging entirely and inserts immediately, as every send did
        # before this setting existed.
        "undo_send_seconds": 5.0,
    },
    SettingCategory.MAIL: {
        # A message the user files into Archive or the spam (Junk) folder
        # -- the toolbar action or a drag-and-drop move alike -- is marked
        # read as it moves (alerts/dispatch.py's own arrival marking is
        # unaffected; this is about the user's moves, not the pipeline's).
        # Also decides whether mail another client puts in Archive is
        # marked read (filing/read_state.py); Trash is always read.
        "mark_read_on_file_to_archive_or_junk": True,
        # Whether the notification bell's badge counts new-mail alerts.
        # Off, it counts system notifications only; the bell's own lists
        # still show every alert either way.
        "bell_badge_counts_new_mail": True,
        # How long a new-mail alert waits for that message's pipeline run
        # (rules/classification, which can still refile it) to reach a
        # terminal status before notifying anyway with whatever folder the
        # message is in by then. Long enough to outlast one
        # pipeline.unavailable_probe_seconds retry cycle of a suspended
        # provider circuit breaker, short enough that a stalled provider
        # never means a silent mailbox for long.
        "notify_wait_seconds": 120.0,
    },
    SettingCategory.ORDERS: {
        # The model that decides where a mail belongs and writes an
        # order's text, called through the provider settings.ai.provider
        # names (pipeline/context.py's ModelGateway). Empty means not
        # chosen: an account cannot be switched on until it is (see
        # api/accounts.py's PATCH handler).
        "model": "",
        # A reasoning model thinks before it answers and pays for that
        # from max_tokens -- at a higher effort it can spend the whole
        # budget and return nothing. Raise max_tokens together with this.
        "reasoning_effort": "none",
        # A ceiling, not a target -- an answer is a few hundred tokens.
        "max_tokens": 4000,
        # The language titles and summaries are written in.
        "language": "English",
        # The first filter: a cheap pattern match that lets through
        # anything that might be an order, ticket or booking, before any
        # model is called -- see orders/filter.py and docs/architecture.md,
        # "Orders". Every entry is a Python regular expression, matched
        # with IGNORECASE against the Subject header (subject), the whole
        # From header (from) or the prepared body (body, orders/content.py).
        # A mail passes when no exclude pattern matches and at least one
        # include pattern does.
        "filter": {
            "include": {
                "subject": [
                    "bestell",
                    r"\border(s|ed|ing)?\b",
                    "auftrag",
                    r"\bkauf|gekauft|einkauf|purchase",
                    r"rechnung|invoice|receipt|quittung|\bbeleg",
                    r"zahlung|bezahl|payment|\bpaid\b",
                    "versand|versendet|verschickt|shipped|shipping|shipment|dispatch|"
                    r"\bsent\b",
                    "sendung|paket|päckchen|parcel|package",
                    "liefer|deliver|zugestellt|zustell|angekommen|arriv",
                    r"abhol|pick.?up|collect|packstation|locker",
                    r"tracking|unterwegs|auf dem (rück)?weg|on (its|the) way",
                    r"retoure|rücksend|ruecksend|rückgabe|\breturn|erstatt|refund|"
                    "gutschrift|umtausch",
                    "storn|cancel",
                    "ticket|eintrittskarte|gästekarte|bordkarte|boarding|fahrkarte|"
                    "fahrschein",
                    "buchung|gebucht|booking|booked|reserv|regist|anmeldung",
                    r"\bflug|flight|check-?in|itinerary|reiseplan|\breise|\btrip\b",
                ],
                "from": [
                    r"amazon\.",
                    r"\bdhl\b|dhl\.",
                    r"\bdpd\b|dpd\.",
                    "hermes",
                    "gls-(group|pakete|germany)|gls paket",
                    r"\bups\b|ups\.com",
                    "fedex",
                    "deutschepost|deutsche-post",
                    "sendcloud",
                    "parcel|paket|versand|shipping|tracking",
                    "paypal",
                    "klarna",
                    "saferpay",
                    "novalnet",
                    "mollie",
                    "stripe",
                    "order|bestell|shop@|store@",
                    "booking|buchung|reserv|ticket",
                    r"bahn\.de|deutschebahn",
                    "flixbus",
                    "eurowings|lufthansa|ryanair|easyjet",
                    "eurostar",
                    "eventim|reservix|ticketmaster",
                ],
                "body": [
                    r"bestell(nummer|nr)|order (number|no\.?|#)|auftrags(nummer|nr)",
                    "sendungs(nummer|verfolgung)|tracking (number|id|code)|paketnummer",
                    "buchungs(nummer|code|referenz)|booking (number|reference|code)|"
                    "reservierungsnummer|confirmation number",
                    r"rechnungs(nummer|nr)|invoice (number|no\.?)",
                    "(your|ihre|deine) (order|bestellung|booking|buchung|reservierung|"
                    "reservation|sendung|shipment)",
                    "(your|ihr|dein) (parcel|package|paket|ticket|kauf|purchase)",
                ],
            },
            "exclude": {
                "from": [
                    r"notifications@github\.com",
                    r"noreply@github\.com",
                ],
            },
        },
        # Worker claim/lease mechanics -- see queue/work_queue.py.
        "lease_seconds": 300,
        "poll_interval_seconds": 2.0,
        "max_attempts": 5,
        "base_delay_seconds": 5.0,
        "max_delay_seconds": 300.0,
    },
}
