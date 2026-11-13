"""
Which folders' mail the pipeline classifies and the semantic index embeds.

A folder is out of scope when its role is one the pipeline never runs on
(sent, drafts, trash, junk, archive) or when `folder_prefs.pipeline_enabled`
is false. A true value records the choice to keep the role's default and
never widens it: a trash folder stays out whatever its preference says.

`FOLDER_IN_PIPELINE_SCOPE_SQL` and `folder_in_pipeline_scope` are the one
definition; every raw-SQL gate in the package interpolates the first and
every Python check calls the second.
"""

from __future__ import annotations

SKIP_ROLES: frozenset[str] = frozenset({"sent", "drafts", "trash", "junk", "archive"})

_SKIP_ROLES_SQL = ", ".join(f"'{role}'" for role in sorted(SKIP_ROLES))

# Expects `folders` aliased `f` and `folder_prefs` aliased `fp` (outer join).
FOLDER_IN_PIPELINE_SCOPE_SQL = (
    f"coalesce(fp.special_use_override, f.special_use, '') NOT IN ({_SKIP_ROLES_SQL}) "
    "AND fp.pipeline_enabled IS DISTINCT FROM false"
)


def folder_in_pipeline_scope(special_use: str | None, pipeline_enabled: bool | None) -> bool:
    """Whether mail in a folder with this effective role and preference is
    classified. `special_use` is the role with the override already applied."""
    return (special_use or "") not in SKIP_ROLES and pipeline_enabled is not False
