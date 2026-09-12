"""
Push channels: what a device can mute (push_subscriptions.muted_channels).

The split is the web bell's Mail and System tabs: new mail on one side,
every other alert kind -- a send stuck on its way out, and any kind added
later -- on the other.
"""

from __future__ import annotations

from typing import Literal

PushChannel = Literal["mail", "system"]
PUSH_CHANNELS: tuple[PushChannel, ...] = ("mail", "system")


def channel_for_kind(kind: str) -> PushChannel:
    """The channel an alert of this kind is pushed on."""
    return "mail" if kind == "mail" else "system"
