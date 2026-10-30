"""orders/carriers.py: the one place a carrier and a number become a link."""

from __future__ import annotations

import pytest

from mail_verdict.orders.carriers import CARRIERS, tracking_url

_NUMBER = "JJD 0001/2 x#9"
_ENCODED = "JJD%200001%2F2%20x%239"


@pytest.mark.parametrize("carrier", [k for k, c in CARRIERS.items() if c.url_template])
def test_every_carrier_with_a_page_builds_an_https_link_holding_the_encoded_number(
    carrier: str,
) -> None:
    url = tracking_url(carrier, _NUMBER)
    assert url is not None
    assert url.startswith("https://")
    assert _ENCODED in url
    assert "{n}" not in url


@pytest.mark.parametrize("carrier", ["amazon", "other", "nonsense", "", None])
def test_a_carrier_without_a_public_page_has_no_link(carrier: str | None) -> None:
    assert tracking_url(carrier, "123456789") is None


def test_a_blank_number_has_no_link() -> None:
    assert tracking_url("dhl", "   ") is None


def test_the_number_is_stripped_and_a_path_or_query_cannot_escape_it() -> None:
    url = tracking_url("dpd", "  01/../x?y=1  ")
    assert url == "https://tracking.dpd.de/status/de_DE/parcel/01%2F..%2Fx%3Fy%3D1"
