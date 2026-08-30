"""
The unified message pipeline: one ordered list of stages every message
passes through, spam classification among them as an ordinary stage
rather than a separate system.

    contracts   -- the Stage protocol, StageOutcome, the Effect union,
                   and the exception vocabulary a stage raises to signal
                   failure (never a success flag)
    message_view -- MessageView, the immutable snapshot a stage sees,
                   loaded fresh at execution time
    context     -- RunContext and everything in it that is not the
                   message: settings, history, verdict, folders, models
    effects     -- applying a stage's declared effects, each guarded
    effect_codec -- the {type: value} JSON encoding effects and traces share
    registry    -- stage `type` string -> implementation
    revisions   -- the append-only pipeline definition history
    runner      -- claims a queued run and executes the current definition
    enqueue     -- turning live arrival into a queued run, plus the
                   watermark and reconciliation covering a listener gap
    stages/     -- the built-in stage types (match, classify)
"""
