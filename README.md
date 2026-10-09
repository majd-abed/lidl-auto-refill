# Lidl Connect auto-refill

Python 3.12+ project for checking remaining data and requesting at most one free
+1 GB refill when the allowance is at or below a configurable threshold.

**Current status: HTTP client implemented from captured requests; mock mode
and dry-run remain the defaults.** The user supplied a HAR and confirmed that
its captured action was the free +1 GB refill. The client uses the observed
GraphQL read/mutation, one-time HTTP login, and encrypted renewable sessions.
Login and renewal have passed live read-only tests. A controlled cloud run
also requested one free refill and verified the allowance increase. The repository
is public so its standard hosted runner jobs are free. The GitHub schedule has
not yet delivered an automatic check. A Supabase timer is being prepared to
trigger the same job every five minutes; activation requires a scoped GitHub
dispatch token. Repository variables control checks and refill permission.

The HAR demonstrated that a refill can be applied even when its request returns
HTTP 500 and the portal displays an error. The script therefore verifies the
allowance after an error response and never resends the mutation. See
[OBSERVED_NETWORK.md](OBSERVED_NETWORK.md) for the sanitized findings and
[DEVTOOLS.md](DEVTOOLS.md) for capture guidance.

Session renewal has now also been captured and reproduced through ordinary
HTTP. It issues new access/refresh tokens and reported a one-hour access-token
lifetime. The newest token pair is encrypted and retained between local or
PostgreSQL-backed runs.
No additional HAR or browser automation is required for the captured flow.

## Run on Windows

From this project directory, with Python 3.12+ installed:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
$env:CLIENT_MODE = 'mock'
$env:DRY_RUN = 'true'
$env:MOCK_REMAINING_DATA = '0.20 GB'
.\.venv\Scripts\python.exe app.py
```

The local `.venv` created during development also works directly with those
commands. Activation is optional. On Linux/macOS use `.venv/bin/python` and
shell environment variables instead.

For a **simulated** refill, set `DRY_RUN=false`. The default verification waits
12 seconds before the first read, then 5 seconds between further reads. A total
of three verification reads are allowed. To simulate a slow backend, set
`MOCK_UPDATE_AFTER_READS=2`. For an unverified refill set
`MOCK_REFILL_OUTCOME=no_change`; the process exits with code 2 and retains a
pending record. `timeout` simulates a refill accepted by the backend before a
connection timeout; `rejected` simulates a rejected request.

Mock allowance changes last only for the lifetime of the process. The safety
database persists across runs. Use a distinct state path for each independent
mock experiment; use and preserve a separate database for the live account.

## Authenticated HTTP dry-run

Generate an encryption key **once**, keep it in your secret manager, and use
the same key on later runs. This example generates it into the current process's
environment without printing it. Losing it requires a fresh login; regenerating
it on every scheduled run prevents reuse of the session.

```powershell
$env:LIDL_TOKEN_STATE_KEY = (& .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:LIDL_TOKEN_STATE_PATH = '.state/live-session.sqlite3'
$env:STATE_DB_PATH = '.state/live-refill.sqlite3'
$env:LIDL_USERNAME = Read-Host 'Lidl phone number'
$taskPassword = Read-Host 'Lidl password' -AsSecureString
$env:LIDL_PASSWORD = [Net.NetworkCredential]::new('', $taskPassword).Password
.\.venv\Scripts\python.exe bootstrap_session.py
Remove-Item Env:\LIDL_USERNAME, Env:\LIDL_PASSWORD
Remove-Variable taskPassword
$env:CLIENT_MODE = 'http'
$env:DRY_RUN = 'true'
.\.venv\Scripts\python.exe app.py
```

The bootstrap obtains published client settings from Lidl's current public
portal JavaScript. It reads the file without executing it and stops if its
location/configuration changes. `LIDL_CLIENT_ID` and `LIDL_CLIENT_SECRET` may
override discovery, using environment variables only. The password is used
for this explicit login and is never saved. CAPTCHA or SMS requirements stop
setup for normal user sign-in; the project does not bypass challenges.

`app.py` renews shortly before access-token expiration. After a read returns
HTTP 401 it may renew once and retry that **read** once. Refill requests are
never replayed, including after HTTP 401. An expired/revoked refresh token or
interrupted renewal requires `bootstrap_session.py --replace` with credentials
supplied again. This replaces only the session; existing refill safety records
remain intact.

For a short run without renewable storage, supply a current raw access token.
Leave `LIDL_TOKEN_STATE_KEY` unset in this mode. PowerShell can read the token
without echoing:

```powershell
$taskAccessToken = Read-Host 'Current raw access token' -AsSecureString
$env:LIDL_ACCESS_TOKEN = [Net.NetworkCredential]::new('', $taskAccessToken).Password
$env:CLIENT_MODE = 'http'
$env:DRY_RUN = 'true'
$env:STATE_DB_PATH = '.state/live-refill.sqlite3'
.\.venv\Scripts\python.exe app.py
Remove-Item Env:\LIDL_ACCESS_TOKEN
```

The HTTP client does not use browser cookies or a browser runtime. Password
login is exclusive to `bootstrap_session.py`; regular checks reuse/renew tokens.
Access-only mode stops when its supplied token expires.
`LIDL_SESSION_TOKEN` is accepted as an alias for the
raw access token, with `LIDL_ACCESS_TOKEN` taking precedence.

The refill mutation is restricted to the captured free-offer identifier
`CCS_92061`; there is no configuration option for selecting a different offer.
This mapping was confirmed for this account's free +1 GB action on 9 October
2026. Revalidate it if the tariff or portal offer changes. No pricing/eligibility
API was exposed in the supplied HAR; the identifier's free status comes from
the user's confirmation, rather than a programmatic live-price check.

## Configuration

All configuration comes from environment variables. `.env.example` documents
the settings and contains placeholders only; the application does **not**
automatically load `.env`. Runtime dependencies are `requests`, `cryptography`,
and `psycopg` for PostgreSQL; `pytest` is the test dependency.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLIENT_MODE` | `mock` | `mock` or the captured `http` client |
| `STATE_BACKEND` | `sqlite` | Local SQLite, or `postgres` for hosted cloud jobs (HTTP mode) |
| `DATABASE_URL` | unset | PostgreSQL URI; required for the cloud backend |
| `DATABASE_CA_CERT` | unset | Optional server CA certificate as PEM text for verified TLS |
| `STATE_ACCOUNT_KEY` | `default` | One stable key per cloud account; workflows use `lidl-main` |
| `LIDL_ACCESS_TOKEN` | unset | Optional raw access token for a short run without renewal |
| `LIDL_TOKEN_STATE_KEY` | unset | Fernet encryption key; enables renewable session storage |
| `LIDL_TOKEN_STATE_PATH` | `.state/session.sqlite3` | Encrypted session; separate from refill state |
| `LIDL_USERNAME`, `LIDL_PASSWORD` | unset | Explicit one-time bootstrap only |
| `LIDL_CLIENT_ID`, `LIDL_CLIENT_SECRET` | unset | Optional paired bootstrap override of published client settings |
| `HTTP_TIMEOUT_SECONDS` | `20` | Connect/read timeout, greater than 0 and at most 120 seconds |
| `DRY_RUN` | `true` | Reads and reports; never sends a refill request or changes a refill record |
| `REFILL_THRESHOLD_GB` | `0.30` | Refill when remaining GB is **at or below** this value |
| `REFILL_COOLDOWN_SECONDS` | `600` | Minimum 600 seconds, even after confirmation |
| `STATE_DB_PATH` | `.state/refill.sqlite3` | Durable SQLite file; one file per account |
| `VERIFY_INITIAL_DELAY_SECONDS` | `12` | Wait before the first verification read |
| `VERIFY_POLL_INTERVAL_SECONDS` | `5` | Wait between verification reads |
| `VERIFY_ATTEMPTS` | `3` | 1–10 read attempts; total configured wait must be at most 300 seconds |
| `MOCK_REMAINING_DATA` | `0.47 GB` | Initial simulated allowance |
| `MOCK_REFILL_OUTCOME` | `success` | `success`, `no_change`, `timeout`, or `rejected` |
| `MOCK_UPDATE_AFTER_READS` | `0` | Number of stale reads before a simulated increase |

Run from the same directory each time, or use an absolute `STATE_DB_PATH`.
Changing the state file or using a separate file in another process defeats
cross-run coordination. A dry-run can initialize the database/schema, but
does not create, confirm, or replace a refill record.

Dry-run may rotate and save authentication tokens. Both the session file and
refill database must persist between checks on the same machine. Each is scoped
to one account; do not share one session file across accounts. Session tokens
and published client settings use authenticated [Fernet encryption](https://cryptography.io/en/latest/fernet/).
Keep its key separate from the database and out of version control. OS file
locks serialize renewals across processes; a durable pending marker is committed
before the token request. If the server rotated tokens but the process crashed
before saving them, later runs stop for a fresh login instead of replaying the
possibly invalid refresh token. SQLite files require a local filesystem with
reliable locking; they are not a distributed lock for separate cloud hosts.
The PostgreSQL backend instead stores encrypted sessions and refill history
in a private schema. Atomic conditional writes commit intent before network
requests, protect token rotation across machines, and preserve pending state
after runner loss. Remote database connections require verified TLS.

`parse_data_allowance()` accepts decimal commas/dots, MB/GB, and whitespace
including nonbreaking spaces. It rejects missing units, multiple numbers,
negative values, infinity, and unexpected text. **1 GB = 1024 MB** matches the
requested `200 MB ≈ 0.195 GB` example. The captured API instead returns numeric
GB values. Its separate parser accepts only the observed GB format, sums the
aggregate `DATA` and `REFILLABLE_DATA` buckets, validates numbers and aware ISO
expiry timestamps, and rejects expired/duplicate/unknown data buckets. Nested
bundle rows are not counted again. An API unit change requires a fresh capture.

## Refill safeguards

1. Read the allowance and validate it. Missing or invalid data stops the run.
2. Reconcile any earlier pending refill only when a later read shows a value
   greater than its recorded starting allowance.
3. At or below the threshold, enforce the cooldown and atomically reserve one
   request using SQLite `BEGIN IMMEDIATE`.
4. **Commit the pending record before sending the refill request.** Other
   processes sharing this database cannot reserve another refill.
5. Call `activate_refill()` once. Only allowance reads are polled afterward,
   even if the refill request times out or returns an error.
6. Confirm only after an observed allowance increase. A successful request
   return alone never counts as confirmation.

A timeout or error is treated as uncertain, then read-only verification runs.
If the allowance increased, the refill is confirmed despite the request error.
Failed verification or process interruption leaves the pending record intact.
**An unverified request remains blocked indefinitely,
including after the cooldown expires.** Later runs can observe and confirm an
increase without issuing another request. This prioritizes duplicate prevention
over automatic recovery.

A crash between recording intent and sending the request can also leave a
pending record even though nothing was sent. There is deliberately no automatic
reset command: resolve this only after inspecting the account/backend and
establishing what happened. Do not delete the state file or configure a new
file to bypass an uncertain live request. A refill consumed before verification
can stay unconfirmed; allowance comparisons cannot prove its absence. The live
client should also use a refill status/transaction identifier if captures reveal
one.

SQLite errors or invalid stored state stop execution. Logs contain numeric
allowances, decisions, and exception types, without full account responses,
request headers, or authentication data. Timestamps are explicitly UTC.

API requests have explicit timeouts, zero transport retries, and no redirect
following. Public client discovery follows only the observed same-host landing
page redirect. HTTP errors, malformed JSON, and GraphQL errors are reported without
logging response contents. The session does not load implicit `.netrc`
credentials or proxy configuration. Timeout behavior follows
[Requests documentation](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts).

Exit codes: `0` for normal no-action/dry-run/cooldown/confirmed outcomes, `1`
for configuration, initial read, state, or unexpected failures, and `2` when a
refill is uncertain or an earlier request remains pending. An exit code 2
requires investigation; rerunning does not submit another refill.

## Validation

- [181 tests passed in GitHub Actions](https://github.com/majd-abed/lidl-auto-refill/actions/runs/37993636398), covering parsing, SQLite/PostgreSQL claims and renewal,
  committed markers surviving process crashes, encryption/key errors,
  HTTP/GraphQL/timeouts, and HTTP 500 after an applied refill.
- An authenticated `CLIENT_MODE=http`, `DRY_RUN=true` execution on 9 October
  2026 read the live allowance and exited with code 0. No mutation was submitted.
- One-time Python HTTP login, token renewal, and a second process reusing the
  encrypted session were validated with live dry-runs. Both token values rotated
  on renewal, the restart reused them, and no refill record was created.
- A hosted runner completed normal API login and saved the encrypted session
  in Supabase. Temporary username/password repository secrets were removed.
- Two fresh hosted runners completed read-only allowance checks before and
  after the controlled refill, reusing the encrypted cloud session.
- A [controlled hosted refill](https://github.com/majd-abed/lidl-auto-refill/actions/runs/37994167864)
  on 9 October 2026 submitted one captured free offer. Lidl returned an error
  response, but read-only verification confirmed an allowance increase and
  committed the confirmed refill state. The mutation was not resubmitted.

## Cloud and GitHub Actions

The project is published at https://github.com/majd-abed/lidl-auto-refill.
Follow [CLOUD_SETUP.md](CLOUD_SETUP.md) for Supabase secrets, one-time bootstrap,
read-only cloud validation, and controlled activation of the schedule.

- `refill.yml` runs tests (including real PostgreSQL integration) and a mock check.
- `bootstrap-cloud.yml` performs an explicit, manual-only login into cloud storage.
- `account-check.yml` supports manual checks and five-minute scheduling.
  Scheduled jobs are skipped until `CLOUD_CHECKS_ENABLED=true`. Refills remain
  disabled unless the manual input or scheduled `REFILL_ENABLED=true` is set.
- `keepalive.yml` updates only `.github/automation-heartbeat.txt` twice a month
  while scheduled checks are enabled, keeping the public repository active.
  It receives no Lidl or database secrets.
- `cloud-timer.yml` prepares, enables, pauses, or inspects the Supabase timer.
  New timers are inactive until explicitly enabled with a scoped dispatch token.

Hosted jobs use `STATE_BACKEND=postgres`, a stable `STATE_ACCOUNT_KEY`, and
repository secrets for `DATABASE_URL` and `LIDL_TOKEN_STATE_KEY`. The newest
encrypted tokens and refill history persist in PostgreSQL across fresh runners.
SQLite remains available for one persistent local machine.

Deployment options:

- One cloud VM/container or self-hosted runner with a persistent local volume,
  where all invocations use the same session/refill files and encryption key.
  Avoid network-mounted SQLite; keep the session `.lock` file alongside it.
- Hosted Actions/serverless jobs use the PostgreSQL adapter and its durable
  atomic claims. Keep the pending marker until verified; a short-lived
  distributed lock alone is insufficient. Supabase's Free plan is the selected
  deployment option; other PostgreSQL providers can use the same adapter.

The included concurrency group serializes these workflow runs and avoids
cancelling an active run. It does not retain refill history or coordinate other
machines. GitHub caches/artifacts alone cannot safely replace the durable
claim: a job may send a refill and crash before uploading state.
[GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

The account workflow declares cron `2-59/5 * * * *` alongside `workflow_dispatch`,
with repository variables gating scheduled execution and refill permission.
All credentials use `${{ secrets.NAME }}`.
Scheduled Actions can be delayed or dropped and run from the default branch;
checks must tolerate irregular intervals.
[GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Never commit credentials, session cookies, token-bearing URLs, HAR files, or
browser storage state. `.gitignore` covers `.env`, local state, captures, and
debug files. The original HAR stays outside the project; a workspace-level
`.gitignore` also excludes HAR files. Current access tokens belong in local
environment variables or GitHub Secrets, never in captured fixtures or docs.

## Project progress

2. Allowance, refill, and token renewal requests captured and documented.
3. HTTP client, encrypted token renewal, and error-after-applied-refill tests implemented.
4. Authenticated HTTP reads tested successfully with `DRY_RUN=true`.
5. One controlled real refill completed and verified from a hosted runner.
6. Supabase/Secrets configured, cloud reads validated, and the repository made
   public for free standard runners. Manual checks work; automatic scheduling
   is awaiting verification. Timer setup and stop controls are in
   [CLOUD_SETUP.md](CLOUD_SETUP.md).

Playwright becomes a runtime dependency only if the captured HTTP flow cannot
be reproduced reliably. Any future fallback must use the same durable claim,
perform one action, wait explicitly, and avoid account screenshots or unredacted
debug artifacts. CAPTCHA and SMS challenges require normal user completion;
no bypassing or interception is planned.
