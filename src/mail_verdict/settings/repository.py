"""
Settings repository: CRUD for the settings table and the per-user
user_settings table.

Each row is one category with a JSONB data blob; a user_settings row is one
user's values of one category.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Setting, UserSetting

logger = logging.getLogger(__name__)


class SettingsRepository:
    """Database operations for the Setting model."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize the settings repository.

        Args:
            db: Database connection
        """
        self._db = db

    async def get_all(self) -> dict[str, dict[str, Any]]:
        """
        Load all settings grouped by category.

        Returns:
            Dict mapping category name to JSONB data
        """
        async with self._db.session() as session:
            result = await session.execute(select(Setting))
            rows = list(result.scalars().all())
        return {row.category: row.data for row in rows}

    async def get_category(self, category: str) -> dict[str, Any] | None:
        """
        Load settings for a single category.

        Args:
            category: Setting category name

        Returns:
            JSONB data dict, or None if not stored
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Setting).where(Setting.category == category)
            )
            row = result.scalar_one_or_none()
        return row.data if row else None

    async def upsert_category(self, category: str, data: dict[str, Any]) -> None:
        """
        Insert or update settings for a category (merge semantics).

        Merges provided keys into existing data, preserving unmentioned keys.

        Args:
            category: Setting category name
            data: Partial or full JSONB data to merge
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Setting).where(Setting.category == category)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                merged = {**existing.data, **data}
                existing.data = merged
            else:
                session.add(Setting(category=category, data=data))
        logger.info("Settings upserted", extra={"category": category})

    async def get_all_user_settings(self) -> dict[uuid.UUID, dict[str, dict[str, Any]]]:
        """
        Load every user's settings, grouped by user and category.

        Returns:
            Dict mapping user id to {category name: JSONB data}
        """
        async with self._db.session() as session:
            result = await session.execute(select(UserSetting))
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row.user_id, {})[row.category] = row.data
        return grouped

    async def upsert_user_category(
        self, user_id: uuid.UUID, category: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Insert or update one user's settings for a category (merge semantics,
        like upsert_category).

        Args:
            user_id: The user the values belong to
            category: Setting category name
            data: Partial or full JSONB data to merge

        Returns:
            The category's stored data for this user after the write
        """
        async with self._db.session() as session:
            existing = await session.get(UserSetting, (user_id, category))
            if existing is not None:
                existing.data = {**existing.data, **data}
                stored = dict(existing.data)
            else:
                session.add(UserSetting(user_id=user_id, category=category, data=data))
                stored = dict(data)
        logger.info(
            "User settings upserted", extra={"category": category, "user_id": str(user_id)},
        )
        return stored
