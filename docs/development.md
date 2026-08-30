# Development

## One-time setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

That is enough to run the test suite. The tests start their own containers, so nothing needs to
be running first.

For frontend work you also need the UI dependencies:

```bash
cd ui && npm install
```

## Running the tests

```bash
pytest                    # everything except the tests that call a real language model
pytest -m unit            # fast, no containers
pytest -m pg              # against a real PostgreSQL and PostIMAP
pytest -m e2e             # full flows, including a real mail server
```

Anything beyond the unit layer needs a container runtime. Docker works; so does rootless Podman,
which needs its socket enabled once:

```bash
systemctl --user enable --now podman.socket
```

The suite finds the socket itself. If it cannot, it fails with that exact command rather than
skipping — a test that quietly skips itself is indistinguishable from one that passes, and this
project does not have any.

Containers are started once per session, not per test, because starting them is slow enough to
dominate the run otherwise.

### The layers

| Marker | What it covers | What it needs |
|---|---|---|
| `unit` | Pure logic: rules, config loading, sanitizers, prompt rendering, cursors | Nothing |
| `pg` | Migrations alongside PostIMAP's schema, the contract version gate, the notification listener, and every write the contract permits | PostgreSQL, PostIMAP |
| `e2e` | Whole flows with the application running in-process: accounts, mail actions, sending, spam, rules, search | The above plus Dovecot and Mailpit |
| `llm` | Classification against the real model, both providers | `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Excluded from CI (`pytest tests/unit/ -m "not llm"`); run it deliberately |

The `llm` layer asserts its keys are present and fails loudly without them.

## Running the application

```bash
cp .dev.env.example .dev.env      # fill in the secrets
podman compose --env-file .dev.env -f compose.dev.yaml up -d
```

This brings up MailVerdict with hot reload, PostIMAP, PostgreSQL, and a throwaway mail world:
Dovecot as the mail server and Mailpit as an SMTP sink that shows you what was actually sent. The
interface is on <http://localhost:18080>.

The production compose file (`compose.yaml`, port 8080) runs the same application without the fake
mail world, against real accounts. The two can run at once.

### Getting mail to work against

The development mail server creates a mailbox for any username on first login, so there are no
accounts to provision — mail just needs delivering:

```bash
python scripts/seed_dev.py
```

That delivers the test corpus — spam, legitimate mail, phishing, an attachment, and a handful of
awkward edge cases — over LMTP, so it arrives as genuinely inbound mail rather than being written
straight into the database. Then add the account through the interface. PostIMAP picks up a new
account without a restart.

Mailpit's own interface shows everything the application sends, so you can check a send worked
without needing a real mailbox anywhere.

## Frontend

```bash
cd ui && npm run build
podman compose --env-file .dev.env -f compose.dev.yaml restart app
```

The static export is served by the backend, which mounts `ui/build` into the container. **The
restart is not optional**: the build replaces that directory rather than writing into it, so the
running container's mount is left pointing at the old one and every page 404s until the container
picks the new directory up.

For faster iteration `npm run dev` runs the Next.js dev server directly against the backend's API,
with no rebuild and no restart.

## Before pushing

```bash
ruff check .
mypy src/
pytest
cd ui && npx tsc --noEmit && npm run build
```

CI runs these as parallel jobs, so a local failure is a CI failure. Check exit codes rather than
reading the last few lines of output — linters print their error count above the final lines, and
reading the tail is how a red run gets mistaken for a green one.

## A note on what you can and cannot write

MailVerdict shares its database with PostIMAP, and PostIMAP enforces what a consumer may write
using PostgreSQL grants. Writing outside that set fails with a permission error rather than
silently doing the wrong thing.

Every permitted write lives in `src/mail_verdict/postimap/actions.py` and nowhere else. If you
find yourself writing SQL against PostIMAP's tables somewhere else, that is the bug — the
statements its triggers depend on need exactly one definition.

The authoritative list of what is readable and writable is PostIMAP's
[consumer contract](https://github.com/frederikb96/postimap/blob/main/docs/consumer-contract.md).

## Releasing

Version lives in `pyproject.toml` and nowhere else. The chart repeats it because Helm requires it,
and a guard fails the release if they disagree:

```bash
python scripts/check_release_versions.py v1.2.3
```

The order matters and is not optional:

- Bump the version and move the changelog's unreleased section under it
- Commit and push the commit
- Wait for CI to pass
- Only then push the tag

The tag is what publishes the image, the chart and the release. Tagging before CI means publishing
something that was never green.
