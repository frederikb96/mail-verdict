"""
Secret store API. Values are write-only: nothing here returns one.

GET    /api/secrets          -- names and timestamps
PUT    /api/secrets/{name}   -- create or replace; body {"value": "..."}
DELETE /api/secrets/{name}   -- remove

A rule's webhook action references a secret by name in a header value
(`{{secret:NAME}}`, webhooks/spec.py). Deleting a secret a rule still
references makes that rule's next delivery fail loudly; it does not
change the rule. Not exposed through MCP: a value passing through a tool
call would be recorded by the client.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from mail_verdict.settings.credentials import EncryptionUnavailableError
from mail_verdict.settings.secret_store import get_secret_repo

router = APIRouter(prefix="/secrets", tags=["secrets"])

_MAX_VALUE_CHARS = 8192


class SecretOut(BaseModel):
    """A stored secret as the API shows it: the name, never the value."""

    name: str
    created_at: datetime
    updated_at: datetime


class SecretWrite(BaseModel):
    """The one place a value travels, inbound only. Validated in the
    handler rather than by the model, because a 422 response echoes the
    rejected input."""

    value: str


class SecretWriteResult(BaseModel):
    """Outcome of a write; `created` is false when it replaced a value."""

    name: str
    created: bool


@router.get("", response_model=list[SecretOut])
async def list_secrets() -> list[SecretOut]:
    """Every stored secret's name and timestamps, by name."""
    return [
        SecretOut(name=s.name, created_at=s.created_at, updated_at=s.updated_at)
        for s in await get_secret_repo().list_all()
    ]


@router.put("/{name}", response_model=SecretWriteResult)
async def put_secret(name: str, request: SecretWrite) -> SecretWriteResult:
    """Create a secret or replace its value."""
    if len(request.value) > _MAX_VALUE_CHARS:
        raise HTTPException(status_code=400, detail="value is too long")
    try:
        created = await get_secret_repo().put(name, request.value)
    except EncryptionUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SecretWriteResult(name=name, created=created)


@router.delete("/{name}", status_code=204)
async def delete_secret(name: str) -> Response:
    """Remove a secret."""
    if not await get_secret_repo().delete(name):
        raise HTTPException(status_code=404, detail="no such secret")
    return Response(status_code=204)
