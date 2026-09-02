#!/usr/bin/env python3
"""Remove test/dev containers whose owning process is confirmed dead.

tests/setup/runtime.py's bootstrap_container_runtime() already runs this sweep at the
start of every pytest session and every scripts/devstack.py invocation -- this script
is the same sweep, standalone, for whenever nothing is about to start a session anyway
and containers are suspected to have accumulated regardless (after several agents have
been stopped, before checking how loaded this machine is, and so on).

    python scripts/prune_orphaned_containers.py

Only ever touches containers carrying this project's own owner labels (see
tests/setup/runtime.py's LABEL_ROLE/LABEL_OWNER_PID/LABEL_OWNER_FINGERPRINT), and only
those whose recorded owner process is provably gone -- never by age, since a genuine
orphan and a legitimately long-running scripts/devstack.py instance accumulate age
identically. compose.dev.yaml's containers never carry these labels at all (podman
compose creates them directly, not through this project's own container-building code),
so the persistent development stack is never a candidate.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.setup.runtime import bootstrap_container_runtime, sweep_orphaned_containers  # noqa: E402


def main() -> int:
    # Resolves DOCKER_HOST if it isn't already, which sweep_orphaned_containers needs
    # to reach the runtime at all -- it also runs its own sweep as a side effect, so
    # calling sweep_orphaned_containers again immediately after finds only what died
    # in between, which is normally nothing; still correct, just usually a no-op.
    bootstrap_container_runtime()
    removed = sweep_orphaned_containers()
    if removed:
        print(f"Removed {len(removed)} container(s) orphaned by a dead process:")
        for name in removed:
            print(f"  {name}")
    else:
        print("Nothing to remove -- no orphaned container found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
