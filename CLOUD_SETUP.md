# Supabase and GitHub Actions setup

Repository: https://github.com/majd-abed/lidl-auto-refill

Scheduled account jobs require the repository variable `CLOUD_CHECKS_ENABLED`.
It starts unset, so scheduled jobs are skipped. Manual account checks default
to dry-run.

The deployed repository completed cloud reads and a controlled refill on
9 October 2026. Both `CLOUD_CHECKS_ENABLED` and `REFILL_ENABLED` are now `true`.
Removing `CLOUD_CHECKS_ENABLED` stops scheduled account and maintenance jobs.

## Create storage

Create a **Free** Supabase project named `lidl-auto-refill` in a European region.
Choose and retain a strong database password; this is independent of Lidl.

Click **Connect**, choose **Session pooler**, and copy the PostgreSQL URI on
port 5432. Replace its password placeholder with the database password, using
URL encoding for special characters. Add the complete URI directly as the
GitHub repository secret `DATABASE_URL`:
https://github.com/majd-abed/lidl-auto-refill/settings/secrets/actions

Keep connection strings and passwords out of chat, source, logs, and workflow
inputs. [Supabase connection instructions](https://supabase.com/docs/guides/database/connecting-to-postgres).

The adapter verifies TLS for remote databases. If the server certificate is
not trusted by system roots, download the server root certificate from
Supabase's **Database settings** and add its PEM contents as the repository
secret `DATABASE_CA_CERT`. The driver uses a temporary certificate file, then
removes it after closing the connection.
[Supabase SSL instructions](https://supabase.com/docs/guides/database/connecting-to-postgres#ssl).

This repository's `DATABASE_CA_CERT` is configured with the production root
certificate served by the Supabase dashboard. Its download URL is published
in the [official dashboard configuration](https://github.com/supabase/supabase/blob/master/apps/studio/hooks/custom-content/custom-content.json).

The database owner login creates a private `lidl_automation` schema and two
tables. Keep this schema outside the exposed Data API schemas. Public schema
privileges are revoked, and row level security has no API policies. The native
database login must own the tables or have the required privileges and RLS
bypass for these internal operations.

## Bootstrap the renewable session

This repository already has a generated `LIDL_TOKEN_STATE_KEY` secret. Keep
that existing key; it was saved directly to GitHub without printing it or
writing a local copy. For a separate deployment, generate a Fernet key once
and add it as `LIDL_TOKEN_STATE_KEY`. This local command captures it without
displaying it:

```powershell
$env:LIDL_TOKEN_STATE_KEY = (& .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

Transfer the key securely to the repository secret of the same name. Supply
`LIDL_USERNAME` and `LIDL_PASSWORD` as temporary repository secrets, then run
**Bootstrap cloud session** manually. This never requests a refill. A normal
CAPTCHA/SMS challenge stops setup for user completion.

If the hosted runner receives HTTP 403 while fetching the public portal page,
set both `LIDL_CLIENT_ID` and `LIDL_CLIENT_SECRET` from the portal's published
client configuration. This skips only the public settings fetch. The normal
password grant and any authentication challenge still apply. These shared
client settings are included in the encrypted session for future renewals.

After success, delete the username/password repository secrets. Regular checks
use the encrypted token pair, database URI, and encryption key. For an
interrupted/revoked session, run bootstrap again with temporary login secrets
and **Replace session** enabled after active jobs end. It never clears pending
refill history.

## Validate cloud reads

Run **Account check** manually with **Allow one free refill** unchecked. Confirm
that it retrieves the allowance. Run again to validate session reuse from a
fresh hosted runner.

The PostgreSQL adapter uses committed, atomic conditional writes. Token rotation
and pending refill claims survive runner loss independently of runner disks,
artifacts, caches, or session-level advisory locks. Database/TLS failures stop
execution without exposing connection strings.

## Controlled refill and scheduling

When a refill is intended and allowance reaches 0.30 GB or below, run **Account
check** manually with **Allow one free refill** enabled. Threshold, ten-minute
cooldown, one-request claim, and read-only verification still apply. The free
offer is fixed to captured identifier `CCS_92061`; revalidate if the tariff changes.

After the controlled refill is confirmed, create repository variables:

| Variable | Value | Effect |
| --- | --- | --- |
| `CLOUD_CHECKS_ENABLED` | `true` | Run account checks approximately every five minutes |
| `REFILL_ENABLED` | `true` | Allow scheduled free refills under the safeguards |

Setting only `CLOUD_CHECKS_ENABLED=true` enables scheduled dry-runs. Removing
it stops scheduled jobs. Removing `REFILL_ENABLED` returns scheduled checks to
dry-run. Manual checks stay dry-run unless their input is enabled.

GitHub schedules are approximate. Private-repository Actions use the account's
included runner minutes or paid usage. Five-minute scheduling corresponds to
approximately 8,640 runs in a 30-day month; check usage before sustained
deployment. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

This repository is public, so standard hosted runners are free. Workflow logs,
including allowance readings, are public; credentials remain in Secrets and
tokens remain encrypted in the private database. GitHub disables public
schedules after 60 days without repository activity. `keepalive.yml` makes a
small timestamp-only commit on the 1st and 15th while scheduled checks are
enabled, keeping the repository active. It has contents-write permission but
receives no account or database secrets. Removing `CLOUD_CHECKS_ENABLED` stops
its scheduled jobs as well.
[GitHub schedule rules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Supabase's Free plan currently includes 500 MB of database storage, supports
two active projects, and pauses projects after a week of inactivity.
[Supabase pricing](https://supabase.com/pricing).

## Integration tests

**Tests and mock dry-run** starts a disposable PostgreSQL service. It tests
simultaneous refill claims/token rotation, runner loss, encrypted persistence,
and denial of API-role reads/writes. The test database is independent of live
Supabase storage and account-check jobs.

## Supabase timer

The GitHub cron has not produced an automatic run despite an active workflow
and enabled gates. Supabase Cron can trigger the same hosted job every five
minutes without depending on GitHub's cron dispatcher. The job still starts
when a GitHub runner becomes available.
[Supabase Cron](https://supabase.com/docs/guides/cron/quickstart).

Run **Manage cloud timer** with action **prepare**. This installs `pg_cron` and
`pg_net`, creates a private dispatch function, and creates an **inactive** job
named `lidl-account-check`. It does not contact Lidl or send a GitHub request.

Create a GitHub **fine-grained personal access token** for resource owner
`majd-abed`, repository access **Only select repositories → lidl-auto-refill**,
and repository permission **Actions → Read and write**. Choose an expiration
and renew the token before it expires. Save the token directly as repository
secret `GITHUB_DISPATCH_TOKEN`; never paste it in source, SQL, or chat.
[Token instructions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Run **Manage cloud timer → enable**. This stores the scoped token in encrypted
Supabase Vault and activates `*/5 * * * *`. The token only starts Actions jobs
in this repository; the normal account job does not receive it. Cron commands
contain a private function call, and the transient HTTP request queue is denied
to public/API roles. The account's encrypted Lidl tokens are unchanged.
[Vault documentation](https://supabase.com/docs/guides/database/vault).

Automatic runs appear as **Automatic account check (Supabase timer)** in
GitHub Actions. They respect `CLOUD_CHECKS_ENABLED` and `REFILL_ENABLED`, including
when someone supplies the manual refill override alongside `scheduled_check`.
An ordinary manual run stays dry-run unless **Allow one free refill** is enabled.
Disable the GitHub cron after the Supabase timer is verified to avoid two timers.

View **Supabase → Integrations → Cron → lidl-account-check → History** to see
timer ticks. A successful tick means the HTTP request was queued; it does not
prove GitHub accepted or completed the account job. **Manage cloud timer → status**
reports the last ticks, HTTP status codes, and accepted GitHub run IDs without
printing request headers or response bodies. HTTP responses remain available
for six hours, and this timer's dispatch/tick history is retained for seven days.

To stop the timer, run **Manage cloud timer → pause**, or toggle the job inactive
in Supabase Cron. Removing `CLOUD_CHECKS_ENABLED` skips automatic account jobs;
the timer still sends dispatches until paused. Removing `REFILL_ENABLED` keeps
checks running as dry-run. These controls never clear pending refill history.
