"""
build_migrated_definition: turning an existing deployment's settings.rules
and settings.spam into the first pipeline revision. Pure function, no
database -- the pg-layer proof that the migration actually runs this code
lives in tests/pg/test_pipeline_runner.py, which exercises the default
(empty-rules) output end to end.
"""

from __future__ import annotations

from mail_verdict.pipeline.revisions import build_migrated_definition


def _stage(document: dict, stage_id: str) -> dict:
    for stage in document["stages"]:
        if stage["stage_id"] == stage_id:
            return stage
    raise AssertionError(f"no stage {stage_id!r} in {document['stages']}")


def test_default_settings_produce_classify_then_move_spam() -> None:
    """No rules, spam enabled with the default auto-move/auto-mark-read --
    the shape every fresh deployment migrates into."""
    document = build_migrated_definition(raw_rules=[], spam_settings={"enabled": True})

    stage_ids = [s["stage_id"] for s in document["stages"]]
    assert stage_ids == ["classify", "move-spam"]

    classify = _stage(document, "classify")
    assert classify["type"] == "classify"
    assert classify["enabled"] is True

    move_spam = _stage(document, "move-spam")
    assert move_spam["config"]["when"] == {"verdict_is": "spam"}
    assert {"move": {"special_use": "junk"}} in move_spam["config"]["effects"]
    assert {"set_flags": {"seen": True}} in move_spam["config"]["effects"]


def test_spam_disabled_produces_a_disabled_classify_stage() -> None:
    """spam.enabled=False must not vanish -- it becomes enabled=False on
    the classify stage, so re-enabling it later is a stage edit, not
    losing the fact it existed."""
    document = build_migrated_definition(raw_rules=[], spam_settings={"enabled": False})

    assert _stage(document, "classify")["enabled"] is False


def test_auto_move_to_junk_disabled_produces_no_move_spam_stage() -> None:
    document = build_migrated_definition(
        raw_rules=[], spam_settings={"enabled": True, "auto_move_to_junk": False},
    )

    stage_ids = [s["stage_id"] for s in document["stages"]]
    assert "move-spam" not in stage_ids


def test_auto_mark_read_disabled_omits_the_set_flags_effect() -> None:
    document = build_migrated_definition(
        raw_rules=[],
        spam_settings={"enabled": True, "auto_move_to_junk": True, "auto_mark_read": False},
    )

    effects = _stage(document, "move-spam")["config"]["effects"]
    assert effects == [{"move": {"special_use": "junk"}}]


def test_a_mail_received_rule_migrates_to_a_match_stage() -> None:
    raw_rules = [{
        "name": "archive-newsletters",
        "trigger": "mail.received",
        "conditions": {"subject_contains": "newsletter"},
        "actions": [{"move_to": "Archive"}],
    }]
    document = build_migrated_definition(raw_rules=raw_rules, spam_settings={})

    migrated = next(
        s for s in document["stages"] if s["type"] == "match" and "archive" in s["name"]
    )
    assert migrated["config"]["when"] == {"subject_contains": "newsletter"}
    assert migrated["config"]["effects"] == [{"move": {"folder_name": "Archive"}}]
    assert migrated["halt"] is False


def test_a_non_mail_received_trigger_is_not_migrated() -> None:
    """The pipeline is triggered by arrival only -- a rule that used to
    react to a move or a delete has nothing left to trigger it, and
    migrating it silently would mean it never fires again with no record
    of why. It is dropped, not carried forward broken."""
    raw_rules = [{
        "name": "junk-exit-tag", "trigger": "mail.moved",
        "conditions": {}, "actions": [{"tag": "reviewed"}],
    }]
    document = build_migrated_definition(raw_rules=raw_rules, spam_settings={})

    stage_names = [s["name"] for s in document["stages"]]
    assert "junk-exit-tag" not in stage_names


def test_a_stop_action_truncates_effects_and_sets_halt() -> None:
    """The old `{"stop": true}` action becomes the stage's own `halt`
    flag; actions listed after it in the old rule never ran either, so
    truncating the effect list at that point reproduces the same
    behaviour rather than a new one."""
    raw_rules = [{
        "name": "stop-rule", "trigger": "mail.received", "conditions": {},
        "actions": [{"tag": "seen-by-rule"}, {"stop": True}, {"move_to": "Never"}],
    }]
    document = build_migrated_definition(raw_rules=raw_rules, spam_settings={})

    migrated = next(s for s in document["stages"] if s["name"] == "stop-rule")
    assert migrated["halt"] is True
    assert migrated["config"]["effects"] == [{"tag": {"add": ["seen-by-rule"]}}]


def test_copy_to_and_forward_to_actions_are_dropped() -> None:
    """Both actions logged "not yet supported" and returned success in the
    old executor -- dead vocabulary, not carried into the new effect set."""
    raw_rules = [{
        "name": "dead-actions", "trigger": "mail.received", "conditions": {},
        "actions": [{"copy_to": "Backup"}, {"forward_to": "someone@example.com"}],
    }]
    document = build_migrated_definition(raw_rules=raw_rules, spam_settings={})

    migrated = next(s for s in document["stages"] if s["name"] == "dead-actions")
    assert migrated["config"]["effects"] == []
