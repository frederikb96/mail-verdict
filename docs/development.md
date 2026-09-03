# Development

## One-time setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

That is enough to run the test suite. The tests start their own containers, so nothing needs to
be running first.

For frontend work, or to run the `ui` test layer, you also need the UI dependencies and a build:

```bash
cd ui && npm install && npm run build
```

The `ui` layer additionally needs Playwright's own browser, once:

```bash
playwright install chromium
```

## Running the tests

```bash
pytest                    # everything except the tests that call a real language model
pytest -m unit            # fast, no containers
pytest -m pg              # against a real PostgreSQL and PostIMAP
pytest -m e2e             # full flows, including a real mail server
pytest -m ui              # the same, driven through a real browser
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

Rootless Podman has no Ryuk-compatible reaper, so a run that gets killed rather than exiting
cleanly (a timeout, a stopped agent, this machine under load) leaves its containers running with
nothing else watching for them. Every container this project's own tooling starts is labelled
with the PID and a liveness fingerprint of the process that started it, and every test session
and `scripts/devstack.py` run sweeps and removes anything so labelled whose owning process is
confirmed dead before starting anything of its own -- never by age, since a genuine orphan and a
`scripts/devstack.py` instance someone is still using accumulate age identically.
`scripts/prune_orphaned_containers.py` runs the same sweep standalone, for whenever containers are
suspected to have accumulated and nothing is about to start a session anyway.

### The layers

| Marker | What it covers | What it needs |
|---|---|---|
| `unit` | Pure logic: rules, config loading, sanitizers, prompt rendering, cursors | Nothing |
| `pg` | Migrations alongside PostIMAP's schema, the contract version gate, the notification listener, and every write the contract permits | PostgreSQL, PostIMAP |
| `e2e` | Whole flows with the application running in-process: accounts, mail actions, sending, spam, rules, search, calendars and contacts | The above plus Dovecot, Mailpit and Radicale |
| `ui` | What the browser does with state: row/reading-pane/keyboard/bulk/drag controls, compose, drafts, SSE-driven refresh, mobile layout | The above plus a built `ui/` and Playwright's Chromium |
| `llm` | Classification against the real model, both providers | `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Excluded from CI (`pytest tests/unit/ -m "not llm"`); run it deliberately |

The `e2e` layer owns state reconciliation -- does an action reach PostIMAP, does a count change,
does the Sent copy come back. The `ui` layer owns what's rendered and what a control actually
sends; a `ui` test that only re-asserts a database row is duplicating `e2e` and should assert the
request body or the DOM instead. It runs the application against a real bound port rather than an
in-process ASGI client, since a browser needs an actual socket to connect to; `pytest tests/ui/`
fails immediately, naming the build command, if `ui/build` doesn't exist or is older than
`ui/src` -- there is no silent skip for a missing frontend build.

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
straight into the database. Each message is stamped with the current date and a fresh Message-ID
on the way out, which is what makes the pipeline treat it as mail that just landed and classify
it; `--keep-dates` delivers the fixtures verbatim instead. Then add the account through the
interface. PostIMAP picks up a new account without a restart.

Mailpit's own interface shows everything the application sends, so you can check a send worked
without needing a real mailbox anywhere.

### Calendars and contacts

The dev stack runs Radicale as a throwaway CalDAV/CardDAV server, the same role Dovecot plays for
mail — no provisioning step: any Basic auth is accepted and the principal is auto-created on first
request. `python scripts/seed_dev.py` seeds a calendar and an address book on it directly, the same
"talk to the throwaway server before any account exists" approach it already uses for mail over
LMTP, and prints what to do next. Then add a DAV account through the interface (or `POST
/api/dav-accounts` directly) with `url` set to `http://radicale:5232/` — the container's own network
alias, since PostIMAP (not this host) is what connects to it — and the username the script seeded
under. PostIMAP mirrors it the same way it mirrors mail, and needs `service_version >= 1.6.0` to do
so, which is what `compose.dev.yaml` and `tests/setup/images.py` both pin.

Pointing a DAV account at a real server instead (Nextcloud, or anything else) still works exactly
as before — Radicale is additive, not a requirement.

### Running more than one stack at once

`compose.dev.yaml` has one fixed name, six fixed host ports, and one fixed pgdata directory, so
only one instance of it can exist on a machine — and two checkouts on different Alembic revisions
can never share its one database, since the application runs `alembic upgrade head` at startup and
refuses to start against two heads. Working in more than one checkout at a time (a second worktree,
a branch under review) needs a second, independent stack rather than a second copy of the compose
file:

```bash
python scripts/devstack.py
python scripts/devstack.py --to bob@test.local
```

This starts Postgres, Dovecot, Mailpit and PostIMAP on a private network with random host ports —
the same containers the `ui` and `e2e` test layers build for themselves — migrates that checkout's
own database, delivers the test corpus, creates and waits for the account, and prints where to
reach the application and Mailpit. Ctrl-C, or a plain `kill` of the process, stops the application
and removes every container it started; nothing it creates is shared with another instance,
including one started from the same checkout a second time. It warns rather than failing if
`ui/build` doesn't exist, since the API alone is often enough — build the frontend first if you
need pages to render.

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

The export is prerendered to static HTML at build time, so anything a component derives from the
clock or from the browser's timezone is baked from the build machine's own. Rendering such a value
during the prerender means the first client render disagrees with the shipped HTML, React reports
a hydration mismatch and rebuilds the tree -- which costs the handlers attached during hydration.
Wrap a view that needs the clock in `ClientOnly` (`ui/src/components/client-only.tsx`), as the
calendar does.

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
