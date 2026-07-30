# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mitake-sms` is 加我科技's internal web tool for sending SMS via the 三竹 (Mitake) API, plus a
balance-alert job. Deployed at <https://sms.chenghyang.uk> (VPS `187.127.109.145`). The SMS point
pool is **shared with the App team** (App uses the same pool to send registration codes), so the
entire codebase optimizes for one thing: never let anyone accidentally spend a point, and never let
a confusing screen cause someone to resend and double-charge.

This repo is **public** — credentials only ever live in `.env` (local, gitignored) or
`/etc/mitake-sms.env` (VPS, mode 600). Never in code, tests, docs, or commit messages.

## Commands

```bash
# Tests (conftest.py blocks all real Mitake API calls by default — see below)
pytest tests/

# Lint
ruff check .

# Offline smoke test of the core module (pure functions only, no network)
python mitake.py

# Run the web app locally (binds 127.0.0.1:8766 by default)
python -m web.server
python -m web.server --dry-run   # build objects only, don't listen — config sanity check

# Run a single test file / test
pytest tests/test_mitake.py
pytest tests/test_web.py -k test_name
```

No `pyproject.toml`/`ruff.toml` — ruff runs with defaults. No `requirements-dev.txt` yet; `pytest`
and `ruff` are assumed to already be on PATH.

### 🔴 Before writing any test: read `tests/conftest.py`'s docstring first

It replaces `mitake._OPENER.open` with a stub that raises on every test by default, because a
forgotten mock here means a test **actually calls the real Mitake API** — real money out of the
shared pool. The interception point is `mitake._OPENER.open`, **not** `urllib.request.urlopen` —
the module builds its own opener to disable redirects, so patching the stdlib function catches
nothing. To let a test hit the network for real, add `@pytest.mark.allow_network`, and only for
`query_balance` (free) — never for `send_sms`.

## Architecture

```
本機開發 (this repo)          n8n2vps-hub (separate repo)
┌──────────────────┐         ┌────────────────────────┐
│ mitake.py         │◄────────│ jobs/job_010_mitake_    │
│ (core, stdlib-only,│  import │ balance/  (daily 08:00  │
│  the ONLY thing     │        │  balance check + alert) │
│  that talks to      │        └────────────────────────┘
│  smsapi.mitake.com  │
│  .tw)                │
│        ▲             │
│        │ import       │
│ web/                 │
│  server.py            │──► Cloudflare Tunnel ──► https://sms.chenghyang.uk
│  templates.py         │
│  audit.py             │
│  recipients.py         │
└──────────────────┘
        ▲
        │ scp recipients.json (producer → consumer, one-way file handoff)
tools/build_recipients.py
(runs on a machine with LAN access to AIHCR/acfh_api — VPS can't reach it directly)
```

**Everything funnels through `mitake.py`.** Two independent consumers — the web app and the
scheduled balance-alert job in the *separate* `n8n2vps-hub` repo — both import this one module
rather than building their own HTTP requests. Every decision that could cost money or mislead a
user lives here once, so fixing it once fixes it everywhere.

### `mitake.py` — core module (stdlib only, zero third-party deps)

- **Pure functions** (no network, fully unit-testable offline): `validate_phone`, `validate_msgid`,
  `count_sms_segments`, `decode_response`, `parse_response`, `parse_status_response`,
  `classify_statuscode`, `describe_delivery_status`.
- **I/O functions** (the only network callers): `query_balance()` (SmQuery, free),
  `send_sms(phone, body, *, max_segments=5)` (SmSend, 1 point/segment — Chinese text is 70
  chars/segment), `query_message_status(msgid)` (SmQuery, free). All go through the module-level
  `_OPENER`, never `urllib.request.urlopen` directly (redirects are disabled deliberately).
- **Error hierarchy**: `MitakeError` → `MitakeConfigError` (missing env vars) /
  `MitakeValidationError` (bad input, guaranteed to fire *before* any network call, never charges) /
  `MitakeAPIError` (Mitake call failed; carries `kind` — one of `ip_blocked` / `auth_failed` /
  `network` / `decode` / `unconfirmed` / `api` / `bad_response` / `msgid_mismatch` — and
  `possibly_charged`).
- **`possibly_charged` is the field callers must branch on**: `False` = Mitake explicitly rejected
  the request, nothing was charged, safe to retry. `True` = the request reached Mitake but the
  result is unconfirmed — it was **probably charged**; retrying risks a double charge and the
  recipient getting two texts. Never collapse both into a generic "failed, please retry" message.
  Note this attribute currently only exists on `MitakeAPIError`, not the base `MitakeError` — code
  reading it off the base class needs `getattr(e, "possibly_charged", ...)`.
- Credentials: `MITAKE_USERNAME` / `MITAKE_PASSWORD` from env only, never logged (query strings are
  scrubbed from exception messages too, since Mitake's auth model puts credentials in the URL).
- Deliberately **no automatic retry** anywhere — whether to resend after a failure is a decision a
  human makes after reading `possibly_charged`.
- Deliberately **does not check `query_balance()` before `send_sms()`** — that would introduce a
  TOCTOU race against the App team draining the same pool between the check and the send.

### `web/` — the send/status HTTP interface (stdlib `http.server`, zero third-party deps)

- `server.py` — routes, two-phase send (`POST /preview` → one-time token → `POST /send`), a sliding
  per-hour segment-based rate limiter, a separate free-query throttle for `/status`, and an
  application-layer header check (`Cf-Access-Authenticated-User-Email`) that is **not real auth**
  (the header is forgeable) — its job is to turn "Cloudflare Access misconfigured" from a silent
  open gateway into a visible 403.
- `templates.py` — HTML generation; all user input goes through `html.escape`. Deliberately does
  **not** import `mitake` (see `web/__init__.py` docstring — import-order fragility isn't worth it
  for a handful of string constants, so a few classification strings are duplicated and locked down
  by tests instead).
- `audit.py` — send audit log (`send-audit.jsonl`, phone numbers masked to last 4 digits); the rate
  limiter replays this file on startup to avoid resetting quotas on every restart.
- `recipients.py` — loads the optional recipient dropdown from `MITAKE_WEB_RECIPIENTS_PATH`;
  missing/empty file is a supported state (falls back to manual phone entry), never a crash.
- Server binds `127.0.0.1` only by default; going public requires both `--host` and
  `--allow-public` explicitly — no accidental exposure via a changed default.

### `tools/build_recipients.py` — the producer half of a file-handoff pattern

The VPS cannot reach the company's internal network (AIHCR / `acfh_api` MySQL), so this script runs
on a machine that *can* reach both the LAN and (via `scp`) the VPS. It scrapes the AIHCR "體驗借出"
Streamlit page (no real `<table>` DOM — parsed from `page.inner_text("body")` split on the row
delimiter `○`) and cross-references the read-only `acfh_api` MySQL table, then atomically writes
`recipients.json` (temp file + `os.replace`) which `web/recipients.py` later reads. It never imports
`web/` or `mitake.py` — the JSON file is the entire contract between producer and consumer. Any new
feature that needs AIHCR/LAN data should follow this same producer → scp → read-only-consumer shape
rather than trying to make the VPS reach the LAN directly. `pymysql`/`playwright` are deliberately
imported lazily inside functions, not at module top, so the pure `match_recipients` function stays
importable/testable without those third-party deps installed.

### Deployment topology (not run from this repo — reference only)

- `deploy/mitake-web.service` is the systemd unit for the web app on the VPS (port 8766, behind a
  Cloudflare Tunnel + Zero Trust Access restricted to one email).
- The balance-alert job lives in the **separate** `n8n2vps-hub` repo
  (`jobs/job_010_mitake_balance/`), scheduled daily at 08:00 Asia/Taipei via APScheduler. Its
  `config.json` holds the alert thresholds — `MITAKE_ALERT_THRESHOLD` in the env file is legacy and
  has no effect.
- Deploying this repo to the VPS is a manual `git pull` + `systemctl restart mitake-web` +
  `systemctl restart n8n2vps-hub` (the hub process must restart too, or it keeps the old
  `mitake.py` in `sys.modules`). `n8n2vps-hub`'s own `deploy.sh` does **not** touch this repo.

## Where the deeper detail lives

- `HANDOFF.md` — module interface details, the `possibly_charged` contract, Mitake API hard rules,
  known test-guard boundaries (§2–3 are required reading before touching `mitake.py` or `web/`).
- `doc/architecture.md` — system diagram, component interaction, and a 12-entry ADR table (every
  major design decision plus its cost) — read before changing anything that looks over-engineered
  for a "just call an SMS API" tool; it almost certainly isn't.
- `doc/session*-summary.md` — chronological log of what changed each session and why.
