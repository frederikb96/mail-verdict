#!/usr/bin/env python3
"""
E2E test runner for MailVerdict.

Manages the full lifecycle: container startup, test execution, result
collection, and cleanup. Follows Prism's runner pattern with per-test
YAML results, fail-fast mode, and graceful shutdown on SIGINT.

Usage:
    python -m tests.e2e.run_e2e
    python -m tests.e2e.run_e2e --only test_full_spam_flow
    python -m tests.e2e.run_e2e --no-lifecycle  # skip container start/stop
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = PROJECT_ROOT / "compose.test.yaml"
RESULTS_DIR = PROJECT_ROOT / "tests" / "e2e" / "results"
E2E_TEST_DIR = PROJECT_ROOT / "tests" / "e2e"

_shutdown_requested = False


def _signal_handler(signum: int, _frame: object) -> None:
    """Handle SIGINT for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    print(f"\n[SIGINT] Graceful shutdown requested (signal {signum})")


@dataclass
class TestResult:
    """Result of a single E2E test execution."""

    name: str
    status: str  # passed, failed, skipped, error
    duration_seconds: float = 0.0
    output: str = ""
    error: str = ""


@dataclass
class RunSummary:
    """Summary of an E2E test run."""

    started_at: str = ""
    finished_at: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errored: int = 0
    results: list[TestResult] = field(default_factory=list)


def discover_tests(
    test_dir: Path,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> list[Path]:
    """
    Discover E2E test files in the test directory.

    Args:
        test_dir: Directory to scan for test_*.py files
        only: If set, only include tests matching these names
        skip: If set, exclude tests matching these names

    Returns:
        Sorted list of test file paths
    """
    test_files = sorted(test_dir.glob("test_*.py"))

    if only:
        test_files = [
            f for f in test_files
            if any(pattern in f.stem for pattern in only)
        ]

    if skip:
        test_files = [
            f for f in test_files
            if not any(pattern in f.stem for pattern in skip)
        ]

    return test_files


def run_single_test(test_file: Path) -> TestResult:
    """
    Execute a single E2E test file via pytest.

    Args:
        test_file: Path to the test file

    Returns:
        TestResult with status, duration, and output
    """
    name = test_file.stem
    print(f"\n{'=' * 60}")
    print(f"  Running: {name}")
    print(f"{'=' * 60}")

    start = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(test_file),
                "-v",
                "--tb=short",
                "--no-header",
                "-x",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(PROJECT_ROOT),
        )
        duration = time.monotonic() - start

        if result.returncode == 0:
            status = "passed"
        elif result.returncode == 5:
            status = "skipped"
        else:
            status = "failed"

        return TestResult(
            name=name,
            status=status,
            duration_seconds=round(duration, 2),
            output=result.stdout,
            error=result.stderr,
        )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return TestResult(
            name=name,
            status="error",
            duration_seconds=round(duration, 2),
            error="Test timed out after 300 seconds",
        )
    except Exception as exc:
        duration = time.monotonic() - start
        return TestResult(
            name=name,
            status="error",
            duration_seconds=round(duration, 2),
            error=str(exc),
        )


def save_result(result: TestResult, results_dir: Path) -> None:
    """
    Save a test result as YAML.

    Args:
        result: Test result to save
        results_dir: Directory to write YAML files to
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"{result.name}.yaml"
    data = {
        "name": result.name,
        "status": result.status,
        "duration_seconds": result.duration_seconds,
        "output": result.output[-2000:] if result.output else "",
        "error": result.error[-2000:] if result.error else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def print_summary(summary: RunSummary) -> None:
    """
    Print a formatted test run summary to stdout.

    Args:
        summary: The run summary to display
    """
    print(f"\n{'=' * 60}")
    print("  E2E Test Summary")
    print(f"{'=' * 60}")
    print(f"  Total:   {summary.total}")
    print(f"  Passed:  {summary.passed}")
    print(f"  Failed:  {summary.failed}")
    print(f"  Skipped: {summary.skipped}")
    print(f"  Errors:  {summary.errored}")
    print(f"{'=' * 60}")

    for r in summary.results:
        icon = {"passed": "OK", "failed": "FAIL", "skipped": "SKIP", "error": "ERR"}
        print(f"  [{icon.get(r.status, '??'):>4}] {r.name} ({r.duration_seconds:.1f}s)")

        if r.status in ("failed", "error") and r.error:
            for line in r.error.strip().splitlines()[-5:]:
                print(f"         {line}")

    print()


def main() -> int:
    """
    Main entry point for the E2E test runner.

    Returns:
        Exit code (0 = all passed, 1 = failures)
    """
    parser = argparse.ArgumentParser(description="MailVerdict E2E Test Runner")
    parser.add_argument(
        "--only",
        nargs="+",
        help="Only run tests matching these patterns",
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        help="Skip tests matching these patterns",
    )
    parser.add_argument(
        "--no-lifecycle",
        action="store_true",
        help="Skip container start/stop (assume already running)",
    )
    parser.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="Continue running tests after a failure",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)

    # Late import to avoid issues when running --help
    from tests.e2e.container_manager import ContainerManager

    manager = ContainerManager(COMPOSE_FILE)
    summary = RunSummary(started_at=datetime.now(timezone.utc).isoformat())

    # Lifecycle: down -> clean -> up -> wait_healthy
    if not args.no_lifecycle:
        try:
            print("[lifecycle] Stopping existing containers...")
            manager.down()
            print("[lifecycle] Cleaning volumes...")
            manager.clean_volumes()
            print("[lifecycle] Starting containers...")
            manager.up()
            print("[lifecycle] Waiting for health checks...")
            manager.wait_healthy(timeout=120)
            print("[lifecycle] All containers healthy.")
        except Exception as exc:
            print(f"[lifecycle] FAILED: {exc}")
            print("[lifecycle] Capturing container logs...")
            for svc in ["app", "postgres", "qdrant", "stalwart"]:
                logs = manager.logs(svc, tail=30)
                if logs.strip():
                    print(f"\n--- {svc} logs ---\n{logs}")
            manager.down()
            return 1

    # Discover and run tests
    test_files = discover_tests(E2E_TEST_DIR, only=args.only, skip=args.skip)
    if not test_files:
        print("[runner] No test files found.")
        return 0

    print(f"\n[runner] Found {len(test_files)} test file(s)")
    summary.total = len(test_files)
    fail_fast = not args.no_fail_fast

    for test_file in test_files:
        if _shutdown_requested:
            print("[runner] Shutdown requested, stopping test run.")
            break

        result = run_single_test(test_file)
        save_result(result, RESULTS_DIR)
        summary.results.append(result)

        if result.status == "passed":
            summary.passed += 1
        elif result.status == "failed":
            summary.failed += 1
            if fail_fast:
                print(f"[runner] Fail-fast: stopping after {result.name}")
                # Capture logs for debugging
                if not args.no_lifecycle:
                    print("[runner] Capturing app logs for failed test...")
                    app_logs = manager.logs("app", tail=50)
                    if app_logs.strip():
                        print(f"\n--- app logs ---\n{app_logs}")
                break
        elif result.status == "skipped":
            summary.skipped += 1
        else:
            summary.errored += 1
            if fail_fast:
                print(f"[runner] Fail-fast: stopping after error in {result.name}")
                break

    summary.finished_at = datetime.now(timezone.utc).isoformat()
    print_summary(summary)

    # Save overall summary
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / "_summary.yaml"
    summary_data = {
        "started_at": summary.started_at,
        "finished_at": summary.finished_at,
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "errored": summary.errored,
    }
    summary_path.write_text(
        yaml.dump(summary_data, default_flow_style=False, sort_keys=False)
    )

    # Cleanup
    if not args.no_lifecycle and not _shutdown_requested:
        print("[lifecycle] Stopping containers...")
        manager.down()

    return 0 if (summary.failed == 0 and summary.errored == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
