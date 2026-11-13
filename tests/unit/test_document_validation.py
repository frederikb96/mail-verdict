"""Hard-validation problems that must be rejected before a pipeline
document ever becomes a revision -- pipeline/document_validation.py."""

from __future__ import annotations

import uuid

import pytest

from mail_verdict.pipeline.document_validation import (
    DocumentValidationError,
    validate_document,
)
from tests.helpers.auth import owner_scope, user_scope


def _document(when: dict) -> dict:
    return {
        "enabled": True,
        "stages": [
            {
                "stage_id": "s1",
                "type": "match",
                "config": {"when": when, "effects": []},
            }
        ],
    }


class TestMultiKeyCondition:
    """A leaf condition with more than one key evaluates only the first
    of them (rules/conditions.py), which reads as AND to anyone writing
    it and silently is not -- rejected at write time instead."""

    def test_two_keys_rejected(self) -> None:
        document = _document({"subject_contains": "x", "sender_domain": "y"})
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document, scope=owner_scope())
        assert "one key" in str(exc_info.value)

    def test_single_key_accepted(self) -> None:
        document = _document({"subject_contains": "x"})
        definitions = validate_document(document, scope=owner_scope())
        assert len(definitions) == 1

    def test_two_keys_inside_all_are_each_fine(self) -> None:
        """The multi-key check is per leaf -- 'all' is the vocabulary's
        own way to combine more than one condition."""
        document = _document(
            {"all": [{"subject_contains": "x"}, {"sender_domain": "y"}]}
        )
        definitions = validate_document(document, scope=owner_scope())
        assert len(definitions) == 1


class TestCompositeKeyWithSibling:
    """`all` / `any` / `not` next to another key: the evaluator would use
    only the composite key and silently drop the sibling, so the shape is
    rejected exactly as a multi-key leaf is."""

    @pytest.mark.parametrize("composite", [
        {"all": [{"subject_contains": "x"}]},
        {"any": [{"subject_contains": "x"}]},
        {"not": {"subject_contains": "x"}},
    ])
    def test_composite_with_sibling_rejected(self, composite: dict) -> None:
        document = _document({"sender_domain": "y", **composite})
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document, scope=owner_scope())
        assert "one key" in str(exc_info.value)

    def test_two_composites_rejected(self) -> None:
        document = _document({"all": [{"subject_contains": "x"}], "not": {"sender_domain": "y"}})
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document, scope=owner_scope())
        assert "one key" in str(exc_info.value)

    def test_nested_composite_with_sibling_rejected(self) -> None:
        document = _document(
            {"any": [{"all": [{"subject_contains": "x"}], "sender_domain": "y"}]}
        )
        with pytest.raises(DocumentValidationError):
            validate_document(document, scope=owner_scope())

    def test_lone_composites_accepted(self) -> None:
        document = _document({"all": [{"not": {"subject_contains": "x"}}]})
        assert len(validate_document(document, scope=owner_scope())) == 1


class TestUnknownConditionType:
    def test_rejected(self) -> None:
        document = _document({"enrichment_tag": "priority"})
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document, scope=owner_scope())
        assert "unknown condition type" in str(exc_info.value)


_WEBHOOK = {"webhook": {"name": "canteen", "url": "https://hooks.example/in"}}


def _stage(effects: list[dict], accounts: list[uuid.UUID] | None = None) -> dict:
    return {
        "enabled": True,
        "stages": [{
            "stage_id": "s1", "type": "match",
            "accounts": [str(a) for a in accounts] if accounts else None,
            "config": {"when": {"subject_contains": "x"}, "effects": effects},
        }],
    }


class TestStageAccountsAreTheAuthors:
    """A stage may name only accounts its author owns -- the check that
    keeps one user's document from probing another's folders."""

    def test_own_accounts_accepted(self) -> None:
        mine = uuid.uuid4()
        scope = user_scope(uuid.uuid4(), account_ids=[mine])
        assert len(validate_document(_stage([], [mine]), scope=scope)) == 1

    def test_someone_elses_account_rejected(self) -> None:
        scope = user_scope(uuid.uuid4(), account_ids=[uuid.uuid4()])
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(_stage([], [uuid.uuid4()]), scope=scope)
        assert "foreign_account" in str(exc_info.value)

    def test_an_admin_is_held_to_their_own_accounts_too(self) -> None:
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(_stage([], [uuid.uuid4()]), scope=owner_scope())
        assert "foreign_account" in str(exc_info.value)

    def test_one_foreign_account_among_own_is_still_rejected(self) -> None:
        mine = uuid.uuid4()
        scope = user_scope(uuid.uuid4(), account_ids=[mine])
        with pytest.raises(DocumentValidationError):
            validate_document(_stage([], [mine, uuid.uuid4()]), scope=scope)

    def test_no_accounts_means_all_of_the_authors(self) -> None:
        scope = user_scope(uuid.uuid4())
        assert len(validate_document(_stage([]), scope=scope)) == 1


class TestWebhookIsAdminOnly:
    def test_user_rejected(self) -> None:
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(_stage([_WEBHOOK]), scope=user_scope(uuid.uuid4()))
        assert "effect_not_allowed" in str(exc_info.value)

    def test_admin_accepted(self) -> None:
        assert len(validate_document(_stage([_WEBHOOK]), scope=owner_scope())) == 1

    def test_user_keeps_the_other_effects_expunge_included(self) -> None:
        effects = [
            {"move": {"special_use": "junk"}}, {"trash": {}}, {"expunge": {}},
            {"set_flags": {"seen": True}}, {"tag": {"add": ["a"]}},
            {"keywords": {"add": ["k"]}}, {"notify": {"text": "hi"}},
        ]
        assert len(validate_document(_stage(effects), scope=user_scope(uuid.uuid4()))) == 1

    def test_problems_of_several_stages_are_collected(self) -> None:
        document = _stage([_WEBHOOK], [uuid.uuid4()])
        document["stages"].append({
            "stage_id": "s2", "type": "match",
            "config": {"when": {"subject_contains": "x"}, "effects": [_WEBHOOK]},
        })
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document, scope=user_scope(uuid.uuid4()))
        assert len(exc_info.value.problems) == 2
