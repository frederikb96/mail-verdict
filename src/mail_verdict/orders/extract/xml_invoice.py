"""
E-invoices as XML: UBL / XRechnung and UN/CEFACT CII (ZUGFeRD, Factur-X) read
into issuer, number, dates, totals and bank details. Any other XML is not
read.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from mail_verdict.orders.extract.model import Read

_UBL_ROOTS = {"Invoice", "CreditNote"}
_CII_ROOT = "CrossIndustryInvoice"
_NOTE_CHARS = 300


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _first(root: ET.Element, *path: str) -> str | None:
    """Text of the first element reached by following local names from root;
    each step may sit at any depth below the previous."""
    nodes = [root]
    for name in path:
        found = [e for n in nodes for e in n.iter() if _local(e.tag) == name and e is not n]
        if not found:
            return None
        nodes = found[:1]
    text = (nodes[0].text or "").strip()
    return text or None


def _lines(pairs: list[tuple[str, str | None]]) -> str:
    return "\n".join(f"{label}: {value}" for label, value in pairs if value)


def _ubl(root: ET.Element) -> str:
    currency = _first(root, "DocumentCurrencyCode") or ""
    total = _first(root, "LegalMonetaryTotal", "TaxInclusiveAmount")
    payable = _first(root, "LegalMonetaryTotal", "PayableAmount")
    tax = _first(root, "TaxTotal", "TaxAmount")
    note = _first(root, "Note")
    return _lines([
        ("Invoice number", _first(root, "ID")),
        ("Issue date", _first(root, "IssueDate")),
        ("Due date", _first(root, "DueDate")),
        ("Order reference", _first(root, "OrderReference", "SalesOrderID")),
        ("Issuer", _first(root, "AccountingSupplierParty", "PartyName", "Name")
         or _first(root, "AccountingSupplierParty", "RegistrationName")),
        ("Customer", _first(root, "AccountingCustomerParty", "PartyName", "Name")
         or _first(root, "AccountingCustomerParty", "RegistrationName")),
        ("Total", f"{total} {currency}".strip() if total else None),
        ("Still payable", f"{payable} {currency}".strip() if payable else None),
        ("Tax", f"{tax} {currency}".strip() if tax else None),
        ("IBAN", _first(root, "PayeeFinancialAccount", "ID")),
        ("Payment terms", _first(root, "PaymentTerms", "Note")),
        ("Note", note[:_NOTE_CHARS] if note else None),
    ])


def _cii(root: ET.Element) -> str:
    currency = _first(root, "InvoiceCurrencyCode") or ""
    total = _first(root, "GrandTotalAmount")
    payable = _first(root, "DuePayableAmount")
    tax = _first(root, "TaxTotalAmount")
    date = _first(root, "IssueDateTime", "DateTimeString")
    return _lines([
        ("Invoice number", _first(root, "ExchangedDocument", "ID")),
        ("Issue date", date),
        ("Issuer", _first(root, "SellerTradeParty", "Name")),
        ("Customer", _first(root, "BuyerTradeParty", "Name")),
        ("Total", f"{total} {currency}".strip() if total else None),
        ("Still payable", f"{payable} {currency}".strip() if payable else None),
        ("Tax", f"{tax} {currency}".strip() if tax else None),
        ("IBAN", _first(root, "PayeePartyCreditorFinancialAccount", "IBANID")),
    ])


def read_invoice_xml(data: bytes) -> Read:
    """
    Read an e-invoice.

    Args:
        data: The XML file

    Returns:
        Method `xml` with the invoice fields; method `none` and no text for
        XML that is not an invoice or declares a DTD or entities

    Raises:
        xml.etree.ElementTree.ParseError: Not well-formed XML
    """
    # An entity declaration is how an XML bomb is built; invoices have none.
    head = data[:65536].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        return Read(method="none", text="")
    root = ET.fromstring(data)
    name = _local(root.tag)
    if name in _UBL_ROOTS:
        return Read(method="xml", text=_ubl(root))
    if name == _CII_ROOT:
        return Read(method="xml", text=_cii(root))
    return Read(method="none", text="")
