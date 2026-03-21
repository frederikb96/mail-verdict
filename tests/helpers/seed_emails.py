"""Send test emails to Stalwart for manual sync testing."""

from __future__ import annotations

import asyncio
import sys

from tests.helpers.seed import seed_test_environment, send_test_emails


async def main() -> None:
    """Seed Stalwart accounts and send test emails."""
    print("Seeding Stalwart accounts...")
    await seed_test_environment()

    print("Sending test emails...")
    count = send_test_emails()
    print(f"Done: {count} emails sent to alice@test.local")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
