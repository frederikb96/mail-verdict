"""The one definition of which folders the pipeline runs on."""

from __future__ import annotations

import pytest

from mail_verdict.pipeline.scope import SKIP_ROLES, folder_in_pipeline_scope


@pytest.mark.parametrize("role", sorted(SKIP_ROLES))
def test_the_roles_the_pipeline_never_runs_on_stay_out_whatever_the_switch_says(role: str) -> None:
    assert not folder_in_pipeline_scope(role, None)
    assert not folder_in_pipeline_scope(role, True)


@pytest.mark.parametrize("role", [None, "", "inbox", "flagged", "all"])
def test_other_folders_run_unless_switched_off(role: str | None) -> None:
    assert folder_in_pipeline_scope(role, None)
    assert folder_in_pipeline_scope(role, True)
    assert not folder_in_pipeline_scope(role, False)
