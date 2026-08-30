"""
PostIMAP integration layer.

The only module in MailVerdict that knows the PostIMAP consumer contract
(``/home/frederik/Programming/postimap/docs/consumer-contract.md`` at
development time; vendored knowledge here at runtime). Every contract write
goes through :mod:`mail_verdict.postimap.actions` so the SQL PostIMAP's
triggers depend on lives in exactly one place.
"""

from __future__ import annotations

from mail_verdict.postimap.contract import SUPPORTED_CONTRACT_VERSION, ContractMismatchError

__all__ = ["SUPPORTED_CONTRACT_VERSION", "ContractMismatchError"]
