"""The webhook action's validation and encoding: what a rule may declare,
and that a credential never has to be typed into one."""

from __future__ import annotations

import pytest

from mail_verdict.pipeline.contracts import Webhook
from mail_verdict.pipeline.document_validation import DocumentValidationError, validate_document
from mail_verdict.pipeline.effect_codec import EffectConfigError, effect_to_dict, parse_effect
from mail_verdict.webhooks.spec import referenced_secrets, render_headers

_VALID = {
    "name": "canteen",
    "url": "https://example.com/api/mails",
    "headers": {"Authorization": "Bearer {{secret:CANTEEN_TOKEN}}"},
    "received_at_param": "received_at",
}


def _with(**changes: object) -> dict[str, object]:
    return {"webhook": {**_VALID, **changes}}


class TestParse:
    def test_round_trips_through_the_codec(self) -> None:
        effect = parse_effect({"webhook": _VALID})
        assert isinstance(effect, Webhook)
        assert effect.method == "POST"
        assert parse_effect(effect_to_dict(effect)) == effect

    @pytest.mark.parametrize(
        "changes",
        [
            {"url": "ftp://example.com/x"},
            {"url": "https://user:pw@example.com/x"},
            {"url": "not a url"},
            {"name": "Has Capitals"},
            {"method": "DELETE"},
            {"body": "json"},
            {"received_at_param": "a b"},
            {"headers": {"Content-Type": "text/plain"}},
            {"headers": {"X-Note": "line\nbreak"}},
            {"unknown_field": 1},
        ],
    )
    def test_invalid_shapes_are_rejected(self, changes: dict[str, object]) -> None:
        with pytest.raises(EffectConfigError):
            parse_effect(_with(**changes))

    @pytest.mark.parametrize("header", ["Authorization", "X-Api-Key", "X-Auth-Token", "Cookie"])
    def test_a_credential_header_must_reference_a_secret(self, header: str) -> None:
        with pytest.raises(EffectConfigError, match="stored secret"):
            parse_effect(_with(headers={header: "literal-token"}))

    def test_a_harmless_header_may_be_literal(self) -> None:
        assert isinstance(parse_effect(_with(headers={"X-Source": "mail"})), Webhook)

    def test_the_error_does_not_echo_a_literal_credential(self) -> None:
        with pytest.raises(EffectConfigError) as exc_info:
            parse_effect(_with(headers={"Authorization": "Bearer hunter2-literal"}))
        assert "hunter2-literal" not in str(exc_info.value)


class TestDocumentValidation:
    def test_a_rule_with_a_bad_webhook_is_rejected_at_write_time(self) -> None:
        document = {
            "enabled": True,
            "stages": [{
                "stage_id": "s1", "type": "match",
                "config": {"when": {}, "effects": [_with(url="nope")]},
            }],
        }
        with pytest.raises(DocumentValidationError, match="webhook"):
            validate_document(document)

    def test_a_valid_webhook_rule_is_accepted(self) -> None:
        document = {
            "enabled": True,
            "stages": [{
                "stage_id": "s1", "type": "match",
                "config": {"when": {"subject_contains": "x"}, "effects": [{"webhook": _VALID}]},
            }],
        }
        assert len(validate_document(document)) == 1


class TestSecretReferences:
    def test_references_are_found_once_each_in_order(self) -> None:
        headers = {"A": "{{secret:ONE}} {{secret:TWO}}", "B": "{{secret:ONE}}", "C": "plain"}
        assert referenced_secrets(headers) == ["ONE", "TWO"]

    def test_render_substitutes_only_the_reference(self) -> None:
        rendered = render_headers({"Authorization": "Bearer {{secret:T}}", "X": "y"}, {"T": "abc"})
        assert rendered == {"Authorization": "Bearer abc", "X": "y"}

    def test_render_with_a_missing_secret_raises_with_the_name_only(self) -> None:
        with pytest.raises(KeyError, match="T"):
            render_headers({"Authorization": "Bearer {{secret:T}}"}, {})
