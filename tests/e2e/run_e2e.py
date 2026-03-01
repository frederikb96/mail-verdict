#!/usr/bin/env python3
"""
E2E test runner for MailVerdict.

Orchestrates E2E test execution with YAML results output.
Supports selective test execution via pytest markers.

Usage:
    python tests/e2e/run_e2e.py                    # Run all E2E tests
    python tests/e2e/run_e2e.py --markers unit     # Run unit tests only
    python tests/e2e/run_e2e.py --markers integration
    python tests/e2e/run_e2e.py --output results.yaml
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"


def run_tests(
    markers: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run pytest with optional marker filtering.

    Args:
        markers: List of pytest markers to filter by
        extra_args: Additional arguments to pass to pytest

    Returns:
        Dict with test results and metadata
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(TESTS_DIR),
        "-v",
        "--tb=short",
    ]

    if markers:
        marker_expr = " or ".join(markers)
        cmd.extend(["-m", marker_expr])

    if extra_args:
        cmd.extend(extra_args)

    start = datetime.now(timezone.utc)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    end = datetime.now(timezone.utc)
    duration = (end - start).total_seconds()

    # Parse output for test counts
    passed = 0
    failed = 0
    errors = 0
    skipped = 0

    for line in result.stdout.splitlines():
        if "passed" in line or "failed" in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "passed" and i > 0:
                    try:
                        passed = int(parts[i - 1])
                    except ValueError:
                        pass
                elif part == "failed" and i > 0:
                    try:
                        failed = int(parts[i - 1])
                    except ValueError:
                        pass
                elif part == "error" and i > 0:
                    try:
                        errors = int(parts[i - 1])
                    except ValueError:
                        pass
                elif part == "skipped" and i > 0:
                    try:
                        skipped = int(parts[i - 1])
                    except ValueError:
                        pass

    return {
        "run_at": start.isoformat(),
        "duration_seconds": round(duration, 2),
        "exit_code": result.returncode,
        "markers": markers or ["all"],
        "summary": {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "total": passed + failed + errors + skipped,
        },
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def write_yaml_results(results: dict[str, Any], output_path: Path) -> None:
    """
    Write test results to a YAML file.

    Args:
        results: Test results dict
        output_path: Path to write YAML output
    """
    yaml_safe = {
        "run_at": results["run_at"],
        "duration_seconds": results["duration_seconds"],
        "exit_code": results["exit_code"],
        "markers": results["markers"],
        "summary": results["summary"],
    }
    output_path.write_text(yaml.dump(yaml_safe, default_flow_style=False, sort_keys=False))


def main() -> int:
    """Run E2E tests and optionally save results."""
    parser = argparse.ArgumentParser(description="MailVerdict E2E test runner")
    parser.add_argument(
        "--markers",
        nargs="+",
        help="Pytest markers to run (e.g., unit integration e2e)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write YAML results (default: stdout summary only)",
    )
    parser.add_argument(
        "extra",
        nargs="*",
        help="Extra arguments passed to pytest",
    )

    args = parser.parse_args()

    results = run_tests(markers=args.markers, extra_args=args.extra or None)

    # Print summary
    summary = results["summary"]
    print(f"\nTest Results ({results['duration_seconds']}s):")
    print(f"  Passed:  {summary['passed']}")
    print(f"  Failed:  {summary['failed']}")
    print(f"  Errors:  {summary['errors']}")
    print(f"  Skipped: {summary['skipped']}")
    print(f"  Total:   {summary['total']}")

    if results["exit_code"] != 0:
        print(f"\nstdout:\n{results['stdout']}")
        if results["stderr"]:
            print(f"\nstderr:\n{results['stderr']}")

    if args.output:
        write_yaml_results(results, args.output)
        print(f"\nResults written to: {args.output}")

    return results["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
