#!/usr/bin/env python3
"""
Opens every carrier's tracking URL with a made-up number and prints the
status and final address, to spot a carrier whose page moved. Network
access only; nothing here runs in CI. Carrier sites answer a made-up
number with 200 and a "not found" page or a redirect, so read the output
rather than expecting an exit code to say it: only 404/410 and transport
errors fail.

Usage: python scripts/check_carrier_links.py
"""

from __future__ import annotations

import sys

import httpx

from mail_verdict.orders.carriers import CARRIERS, tracking_url

_NUMBER = "00340434161094042"


def main() -> int:
    failed = 0
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
        for key in CARRIERS:
            url = tracking_url(key, _NUMBER)
            if url is None:
                print(f"{key:14} no public page")
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                print(f"{key:14} ERROR {type(exc).__name__}: {exc}")
                failed += 1
                continue
            print(f"{key:14} {response.status_code} {response.url}")
            if response.status_code in (404, 410):
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
