"""The committed API contract snapshot matches the code, and the check notices when it doesn't.

CI runs `scripts/export_api_contract.py --check`; this is the same check, run
where a failing diff is cheapest to read.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_api_contract.py"


@pytest.fixture(scope="module")
def exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_api_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_snapshot_is_current(exporter: ModuleType) -> None:
    diffs = exporter.stale_files(exporter.DEFAULT_DIR)
    assert not diffs, "Regenerate with: python scripts/export_api_contract.py\n" + "".join(
        diffs.values()
    )


def test_check_fails_on_a_renamed_schema_field(
    exporter: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copytree(exporter.DEFAULT_DIR, tmp_path, dirs_exist_ok=True)
    openapi_path = tmp_path / "openapi.json"
    document = json.loads(openapi_path.read_text(encoding="utf-8"))
    properties = document["components"]["schemas"]["AlertResponse"]["properties"]
    properties["kind_renamed"] = properties.pop("kind")
    openapi_path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    assert exporter.main(["--check", "--dir", str(tmp_path)]) == 1
    assert "kind_renamed" in capsys.readouterr().out


def test_check_fails_on_a_missing_event_name(exporter: ModuleType, tmp_path: Path) -> None:
    shutil.copytree(exporter.DEFAULT_DIR, tmp_path, dirs_exist_ok=True)
    events_path = tmp_path / "sse-events.json"
    names = json.loads(events_path.read_text(encoding="utf-8"))
    events_path.write_text(json.dumps([n for n in names if n != "alert.new"]), encoding="utf-8")

    assert exporter.main(["--check", "--dir", str(tmp_path)]) == 1


def test_a_version_bump_alone_leaves_the_snapshot_current(
    exporter: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release changes the package version and nothing else; that must not
    make every release commit carry a snapshot diff."""
    monkeypatch.setattr("mail_verdict.server.__version__", "99.0.0")
    assert exporter.main(["--check"]) == 0
