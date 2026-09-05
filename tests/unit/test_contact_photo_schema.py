"""Tests for the server-side bound on an uploaded contact photo -- a
backstop against an unbounded upload reaching storage and the CardDAV
server it syncs to, independent of whatever downscaling the client does
first."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mail_verdict.api.schemas import ContactCreateRequest, ContactEmailIO, ContactUpdateRequest

_ADDRESSBOOK_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _create_request(photo_data_url: str) -> ContactCreateRequest:
    return ContactCreateRequest(
        addressbook_id=_ADDRESSBOOK_ID,
        summary="Test Contact",
        emails=[ContactEmailIO(email="test@example.com")],
        photo_data_url=photo_data_url,
    )


class TestContactPhotoSizeBound:
    def test_an_ordinary_sized_photo_is_accepted(self) -> None:
        request = _create_request("data:image/png;base64," + "a" * 1000)
        assert request.photo_data_url is not None

    def test_a_photo_far_past_the_bound_is_rejected_on_create(self) -> None:
        oversized = "data:image/jpeg;base64," + "a" * 2_000_000
        with pytest.raises(ValidationError):
            _create_request(oversized)

    def test_a_photo_far_past_the_bound_is_rejected_on_update(self) -> None:
        oversized = "data:image/jpeg;base64," + "a" * 2_000_000
        with pytest.raises(ValidationError):
            ContactUpdateRequest(photo_data_url=oversized)
