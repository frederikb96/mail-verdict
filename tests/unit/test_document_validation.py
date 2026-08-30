"""Hard-validation problems that must be rejected before a pipeline
document ever becomes a revision -- pipeline/document_validation.py."""

from __future__ import annotations

import pytest

from mail_verdict.pipeline.document_validation import (
    DocumentValidationError,
    validate_document,
)


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
            validate_document(document)
        assert "one key" in str(exc_info.value)

    def test_single_key_accepted(self) -> None:
        document = _document({"subject_contains": "x"})
        definitions = validate_document(document)
        assert len(definitions) == 1

    def test_two_keys_inside_all_are_each_fine(self) -> None:
        """The multi-key check is per leaf -- 'all' is the vocabulary's
        own way to combine more than one condition."""
        document = _document(
            {"all": [{"subject_contains": "x"}, {"sender_domain": "y"}]}
        )
        definitions = validate_document(document)
        assert len(definitions) == 1


class TestUnknownConditionType:
    def test_rejected(self) -> None:
        document = _document({"enrichment_tag": "priority"})
        with pytest.raises(DocumentValidationError) as exc_info:
            validate_document(document)
        assert "unknown condition type" in str(exc_info.value)
