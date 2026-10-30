"""
Tracking links, built by code. The model names a carrier and a number
(order_facts.carrier, order_facts.value); this module is the one place a
carrier becomes a URL, so a link is never model-written text.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Carrier:
    name: str
    # {n} is replaced by the URL-encoded number; None when the carrier has
    # no public tracking page (Amazon's needs a login).
    url_template: str | None


CARRIERS: dict[str, Carrier] = {
    "dhl": Carrier(
        "DHL", "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode={n}",
    ),
    "dhl_express": Carrier(
        "DHL Express", "https://www.dhl.com/de-de/home/tracking.html?tracking-id={n}",
    ),
    "deutsche_post": Carrier(
        "Deutsche Post", "https://www.deutschepost.de/de/s/sendungsverfolgung.html?piececode={n}",
    ),
    "dpd": Carrier("DPD", "https://tracking.dpd.de/status/de_DE/parcel/{n}"),
    "hermes": Carrier(
        "Hermes", "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation/#{n}",
    ),
    "gls": Carrier("GLS", "https://www.gls-pakete.de/sendungsverfolgung?match={n}"),
    "ups": Carrier("UPS", "https://www.ups.com/track?loc=de_DE&tracknum={n}"),
    "fedex": Carrier("FedEx", "https://www.fedex.com/fedextrack/?trknbr={n}"),
    "tnt": Carrier(
        "TNT",
        "https://www.tnt.com/express/de_de/site/shipping-tools/tracking.html"
        "?searchType=con&cons={n}",
    ),
    "postnl": Carrier("PostNL", "https://jouw.postnl.nl/track-and-trace/{n}"),
    "bpost": Carrier("bpost", "https://track.bpost.cloud/btr/web/#/search?itemCode={n}"),
    "amazon": Carrier("Amazon", None),
    "other": Carrier("Other", None),
}


def tracking_url(carrier: str | None, number: str) -> str | None:
    """
    Args:
        carrier: A key of CARRIERS, or None
        number: The tracking number as written

    Returns:
        The carrier's tracking page for the number, or None when the
        carrier is unknown or has no public page
    """
    entry = CARRIERS.get(carrier or "")
    number = number.strip()
    if entry is None or entry.url_template is None or not number:
        return None
    return entry.url_template.replace("{n}", quote(number, safe=""))
