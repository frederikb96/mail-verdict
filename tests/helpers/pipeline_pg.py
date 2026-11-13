"""Seeding users and account ownership for the pg tests of per-user pipeline
documents. A user row must exist wherever `pipeline_revisions.user_id` is
written; an account runs its owner's document only once it has an owner."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from mail_verdict.database.connection import DatabaseConnection


async def insert_user(db: DatabaseConnection, *, role: str = "user") -> uuid.UUID:
    """A new user row, returning its id."""
    user_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, display_name, role) VALUES (:id, :name, :role)"),
            {"id": user_id, "name": f"user-{user_id}", "role": role},
        )
    return user_id


async def own_account(db: DatabaseConnection, account_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Make `user_id` the owner of an account seeded directly in SQL."""
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO account_owners (account_id, user_id) VALUES (:a, :u) "
                "ON CONFLICT (account_id) DO UPDATE SET user_id = :u"
            ),
            {"a": account_id, "u": user_id},
        )
