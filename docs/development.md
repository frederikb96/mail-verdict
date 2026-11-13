# Development

## One-time setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

The orders OCR tests also need Tesseract with the languages named in `extraction.ocr_languages`
(`config/config.yaml`); on Debian or Ubuntu that is
`apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-nld`. Those tests fail
rather than skip when it is missing.

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
pytest tests/unit         # fast, no containers
pytest tests/pg           # against a real PostgreSQL and PostIMAP
pytest tests/e2e          # full flows, including a real mail server
pytest tests/ui/test_mail_actions_ui.py   # the same, driven through a real browser
```

One layer per invocation, and one module at a time within the browser layer -- see
"Before pushing" for why a single invocation over several of them is a different and
unsupported thing.

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

The same leak applies to the Docker network each session's containers share -- nothing sweeps that
the way containers are swept, so hundreds can accumulate across enough killed runs. The symptom is
not a startup failure: every container comes up and every readiness check passes, but two
containers on a *freshly created* network can no longer reach each other -- a subnet-pool
exhaustion that manifests as one specific container-to-container connection failing (in this
project, most visibly PostIMAP reaching Radicale) while the host's own connections to every
container keep working. `podman network ls | wc -l` in the hundreds, with `podman ps -a` showing
nothing currently running, is the tell; `podman network prune -f` only removes networks with no
attached container, so it is safe to run even with other sessions' work nearby, as long as nothing
of theirs is actively running at that moment.

### The layers

| Marker | What it covers | What it needs |
|---|---|---|
| `unit` | Pure logic: rules, config loading, sanitizers, prompt rendering, cursors | Nothing |
| `pg` | Migrations alongside PostIMAP's schema, the contract version gate, the notification listener, and every write the contract permits | PostgreSQL, PostIMAP |
| `e2e` | Whole flows with the application running in-process: accounts, mail actions, sending, spam, rules, search, calendars and contacts | The above plus Dovecot, Mailpit and Radicale |
| `ui` | What the browser does with state: row/reading-pane/keyboard/bulk/drag controls, compose, drafts, SSE-driven refresh, mobile layout | The above plus a built `ui/` and Playwright's Chromium |
| `llm` | Classification against the real model on each provider, and the orders decide and write calls on Claude and on Infomaniak | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `CUSTOM_AI_API_KEY`. Excluded from CI (`pytest tests/unit/ -m "not llm"`); run it deliberately |

The `e2e` layer owns state reconciliation -- does an action reach PostIMAP, does a count change,
does the Sent copy come back. The `ui` layer owns what's rendered and what a control actually
sends; a `ui` test that only re-asserts a database row is duplicating `e2e` and should assert the
request body or the DOM instead. It runs the application against a real bound port rather than an
in-process ASGI client, since a browser needs an actual socket to connect to; `pytest tests/ui/`
fails immediately, naming the build command, if `ui/build` doesn't exist or is older than
`ui/src` -- there is no silent skip for a missing frontend build.

The `llm` layer asserts its keys are present and fails loudly without them.

### Who a test acts as

The application validates a token on every API request, so every test session runs its own token
issuer: `tests/setup/auth.py` generates a key, serves its key set on a loopback port, and points
the application's `auth:` config at it through the environment, with `OWNER_SUBJECT` as the
instance owner. Mint a token for any subject with the `auth_issuer` fixture
(`auth_issuer.mint("someone")`); a new subject becomes an ordinary user on its first request.

- `e2e`: `app_client` sends the owner's token by default; pass `headers=bearer(token)` to act as
  someone else.
- `ui`: the app is served so that a request without a token acts as the owner -- the browser
  cannot add a header to every request it makes, its event stream included.
- `pg` tests mount bare routers with no gate in front: `act_as(app, owner_scope(...))` from
  `tests/helpers/auth.py` answers every `Depends(current_scope)`, and `as_principal(...)` covers
  code that resolves its own scope (MCP tools called through an in-memory client).
- MCP tools called over HTTP, with a token per call: `tests/helpers/mcp_http.py`.

`tests/e2e/test_multiuser_isolation.py` checks every route and MCP tool as one user against
another's data; [architecture.md](architecture.md#access) describes the model, and
`tests/e2e/isolation/__init__.py` how to classify a new route.

The orders prompts are measured separately, by hand: `scripts/eval_orders.py` runs known-answer cases
against real models and `tests/eval/orders/README.md` explains the harness and how to change a prompt
with it. CI scores the recorded answers offline (`tests/unit/test_eval_scorer.py`).

## Running the application

```bash
cp .dev.env.example .dev.env      # fill in the secrets
podman compose --env-file .dev.env -f compose.dev.yaml up -d
```

This brings up MailVerdict with hot reload, PostIMAP, PostgreSQL, and a throwaway mail world:
Dovecot as the mail server and Mailpit as an SMTP sink that shows you what was actually sent. The
interface is on <http://localhost:18080>.

The application validates an OpenID Connect token on every API request and has no way to switch
that off, so this stack needs an issuer: set `MAIL_VERDICT_AUTH_ISSUER`, `..._JWKS_URL`,
`..._AUDIENCES` and `..._OWNER_SUBJECTS` in `.dev.env`, and reach the API with a token from that
issuer. `scripts/devstack.py` (below) needs none of that -- it runs its own issuer -- and is the
quicker way to a working stack.

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
python scripts/devstack.py --as second
```

This starts Postgres, Dovecot, Mailpit and PostIMAP on a private network with random host ports —
the same containers the `ui` and `e2e` test layers build for themselves — migrates that checkout's
own database, delivers the test corpus, creates and waits for the account, and prints where to
reach the application and Mailpit. Ctrl-C, or a plain `kill` of the process, stops the application
and removes every container it started; nothing it creates is shared with another instance,
including one started from the same checkout a second time. It warns rather than failing if
`ui/build` doesn't exist, since the API alone is often enough — build the frontend first if you
need pages to render.

It runs its own token issuer, and serves the application so that a request without a token -- the
browser's, or a plain `curl` -- acts as one user: the instance owner (admin) by default, or an
ordinary second user with `--as second`. The seeded accounts belong to that user. Both users'
tokens are printed at startup; a request carrying one as `Authorization: Bearer ...` acts as that
user instead, which is how to look at the same stack as the other one.

### A corpus the size of a real account

```bash
python scripts/devstack.py --large
```

The default corpus is seventeen messages, four events and three contacts — enough to see a screen
render, not enough to see it behave. `--large` additionally seeds what a personal account actually
holds: over a thousand threaded messages across several folders, thirty calendar collections of
which most are to-do-only (what a Nextcloud task list is), a few thousand calendar objects
including series that started years ago, and a few thousand contacts across several address books,
a fifth of them carrying an embedded photo and grouped both ways an address book groups people.
Seeding and the first sync take several minutes.

`scripts/seed_large.py` does the seeding and runs against any stack on its own, given the host
ports to reach; `--scale` shrinks every count proportionally.

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

The default, for an ordinary change — seconds to a couple of minutes:

```bash
ruff check .
mypy src/
python scripts/export_api_contract.py --check
pytest tests/unit -m "not llm"
cd ui && npx tsc --noEmit && npm run build     # only if the frontend changed
```

Then push, and let CI run the rest: it covers lint, the unit, pg and e2e layers, the UI type
check, its unit tests and the production build, and the image and chart builds, as parallel jobs
in about two minutes. Checks run on pull requests only; `main` accepts rebase merges of a branch
whose `CI ok` check passed against the current `main`, and a release is cut from a tag, so a red run
is caught with nothing shipped.

Add the layer that covers what the change actually touched — `pytest tests/pg` for a query or a
migration, `pytest tests/e2e` for a route, the one or two `tests/ui` modules covering a screen you
changed. Each browser module rebuilds five containers before its first assertion, so the whole
directory is half an hour and is worth it only when the change reaches across the interface.

The full sweep below is for a change whose blast radius is genuinely wide — a contract every area
reads, a dependency upgrade, anything touching deletion or the mail server:

```bash
pytest tests/pg && pytest tests/e2e
for f in tests/ui/test_*.py; do pytest "$f" || break; done
```

Two things are never worth skipping, because they are cheap and they gate a release: do not tag
before CI is green, and run the tests covering anything irreversible — deletion, expunge, a write
to the mail server — watching them fail first.

One layer per invocation, which is also how CI runs them. A single invocation over the
whole tree is not the same thing and is not supported: the layers share one set of
containers for the run, so every account a test leaves behind — including the
deliberately unreachable ones the error-handling tests create — keeps retrying inside one
mail service for the rest of it, and the accumulated reconnect traffic starves the
calendar and contact syncs of later tests. The failures that produces look like defects in
whichever tests happen to run last.

The browser layer needs it one module at a time for the same reason: its own modules share a set
of containers across the directory, and running all of them together fails the ones that run
last.

CI runs these as parallel jobs, so a local failure is a CI failure. Check exit codes rather than
reading the last few lines of output — linters print their error count above the final lines, and
reading the tail is how a red run gets mistaken for a green one.

## The API contract snapshot

`docs/api-contract/` holds the `/api` OpenAPI document and the list of SSE event names, generated
from the code. Any change to a schema, a route or an event name changes it:

```bash
python scripts/export_api_contract.py           # regenerate, then commit the result
python scripts/export_api_contract.py --check   # what CI runs: a diff and exit 1 if stale
```

The package version is written as `0.0.0`, so a release bump alone leaves it unchanged. A
FastAPI or Pydantic upgrade can change how the document is rendered with no code change here;
regenerate and read the diff before committing it. Changing the snapshot is also the trigger for
[IOS_PARITY.md](IOS_PARITY.md).

A new SSE event name goes into `SSE_EVENT_TYPES` (`api/events.py`) first; `EventRing.add` refuses
anything else.

## A note on what you can and cannot write

MailVerdict shares its database with PostIMAP, and PostIMAP enforces what a consumer may write
using PostgreSQL grants. Writing outside that set fails with a permission error rather than
silently doing the wrong thing.

Every permitted write lives in `src/mail_verdict/postimap/actions.py` and nowhere else. If you
find yourself writing SQL against PostIMAP's tables somewhere else, that is the bug — the
statements its triggers depend on need exactly one definition.

The authoritative list of what is readable and writable is PostIMAP's
[consumer contract](https://github.com/frederikberg/postimap/blob/main/docs/consumer-contract.md).

## Releasing

Version lives in `pyproject.toml` and nowhere else. The chart repeats it because Helm requires it,
and a guard fails the release if they disagree:

```bash
python scripts/check_release_versions.py v1.2.3
```

The order matters and is not optional:

- Bump the version and move the changelog's unreleased section under it
- Open a pull request with the bump and rebase-merge it once CI is green
- Fast-forward the local `main`, then tag the merged commit and push the tag

The tag is what publishes the image, the chart and the release. Tagging before the merge means
publishing something that was never green.

A release that carries a migration cannot be undone by deploying the previous image. The image
runs `alembic upgrade head` when it starts, the database already holds a revision that image does
not contain, and alembic stops with `Can't locate revision`, so the pod never becomes ready. To go
back, run `alembic downgrade <previous revision>` from the newer image first (a downgrade that
rewrites a column takes about as long as the upgrade did), then deploy the previous image.
