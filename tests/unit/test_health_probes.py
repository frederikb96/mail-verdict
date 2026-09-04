"""
Test that health probes have the correct behavior.

Liveness probe: simple process check, never waits on resources
Readiness probe: checks contract status with timeout, doesn't block on pool
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_liveness_probe_never_touches_database() -> None:
    """Verify /api/health/live endpoint exists and does not access database.

    By inspection of server.py, liveness returns immediately without any
    database operations.
    """
    server_py = (REPO_ROOT / "src" / "mail_verdict" / "server.py").read_text()

    # Find the health_live endpoint
    match = re.search(
        r'@api_router\.get\("/health/live"\).*?async def health_live.*?return JSONResponse\('
        r'[^)]*\)',
        server_py,
        re.DOTALL,
    )
    assert match, "health_live endpoint not found"
    endpoint_code = match.group(0)

    # Verify it doesn't call any database methods
    assert "db." not in endpoint_code, "liveness probe must not access database"
    assert "health_check" not in endpoint_code, "liveness probe must not call health_check"
    assert "get_db_connection" not in endpoint_code, "liveness probe must not get connection"


def test_readiness_probe_uses_timeout() -> None:
    """Verify /api/health endpoint uses timeout to avoid blocking on pool.

    When database pool is exhausted, readiness should not timeout waiting
    for a connection. It uses asyncio.wait_for with a 500ms timeout.
    """
    server_py = (REPO_ROOT / "src" / "mail_verdict" / "server.py").read_text()

    # Find the health endpoint - look for the async def health function
    # that comes after the @api_router.get("/health") decorator
    health_section = re.search(
        r'@api_router\.get\("/health"\).*?async def health\(\)',
        server_py,
        re.DOTALL,
    )
    assert health_section, "health endpoint decorator not found"

    # Look for asyncio.wait_for usage after the health function definition
    health_impl = re.search(
        r'async def health\(\).*?(?=\n    @api_router|\n    from mail_verdict)',
        server_py,
        re.DOTALL,
    )
    assert health_impl, "health endpoint implementation not found"
    endpoint_code = health_impl.group(0)

    # Verify it uses asyncio.wait_for with timeout
    assert "asyncio.wait_for" in endpoint_code, (
        "readiness should use asyncio.wait_for to timeout"
    )
    assert "timeout=" in endpoint_code, "readiness should specify a timeout"

    # Verify it checks _contract_ok flag
    assert "_contract_ok" in endpoint_code, "readiness should check contract flag"


def test_helm_values_probe_configuration() -> None:
    """Verify Helm chart probe configuration matches our implementation.

    Readiness should use /api/health endpoint and have appropriate timeout
    and failure threshold to avoid removing pod during transient issues.
    """
    values_yaml = (REPO_ROOT / "charts" / "mail-verdict" / "values.yaml").read_text()

    # Check liveness probe config
    assert "livenessProbe:" in values_yaml, "liveness probe not configured"
    liveness_section = re.search(
        r"livenessProbe:.*?(?=\n\w+:|$)",
        values_yaml,
        re.DOTALL,
    )
    assert liveness_section, "liveness probe section not found"
    liveness = liveness_section.group(0)
    assert "/api/health/live" in liveness, "liveness must use /api/health/live"

    # Check readiness probe config
    assert "readinessProbe:" in values_yaml, "readiness probe not configured"
    readiness_section = re.search(
        r"readinessProbe:.*?(?=\n\w+:|$)",
        values_yaml,
        re.DOTALL,
    )
    assert readiness_section, "readiness probe section not found"
    readiness = readiness_section.group(0)
    assert "/api/health" in readiness, "readiness must use /api/health"
    # Verify timeoutSeconds is set and reasonable
    timeout_match = re.search(r"timeoutSeconds:\s*(\d+)", readiness)
    assert timeout_match, "readiness must have timeoutSeconds"
    timeout = int(timeout_match.group(1))
    assert timeout >= 5, f"timeout should be at least 5s, got {timeout}s"
