"""Manage the Supabase timer without exposing credentials or contacting Lidl."""

import argparse
import json
import os
from pathlib import Path
import re

from config import Config, ConfigurationError
from postgres_store import _Postgres
from state_store import StateStoreError

JOB_NAME = "lidl-account-check"
JOB_COMMAND = "SELECT lidl_automation.dispatch_account_check();"


def validate_dispatch_settings(token: str, repository: str) -> None:
    if not re.fullmatch(r"github_pat_[A-Za-z0-9_]+", token):
        raise ConfigurationError("Save a fine-grained GitHub token as DISPATCH_TOKEN.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ConfigurationError("GITHUB_REPOSITORY must identify the owner and repository.")


def prepare(connection) -> None:
    connection.execute(Path(__file__).with_name("sql").joinpath("scheduler.sql").read_text(encoding="utf-8"))
    # Preparation never activates a new timer and preserves an existing timer.
    if connection.execute("SELECT jobid FROM cron.job WHERE jobname = %s", (JOB_NAME,)).fetchone() is None:
        job_id = connection.execute("SELECT cron.schedule(%s, '*/5 * * * *', %s)", (JOB_NAME, JOB_COMMAND)).fetchone()[0]
        connection.execute("SELECT cron.alter_job(%s, active := false)", (job_id,))


def enable(connection, token: str, repository: str) -> None:
    validate_dispatch_settings(token, repository)
    for name, value in (("lidl_github_dispatch_token", token), ("lidl_github_repository", repository)):
        existing = connection.execute("SELECT id FROM vault.secrets WHERE name = %s", (name,)).fetchone()
        if existing:
            connection.execute("SELECT vault.update_secret(%s, %s)", (existing[0], value))
        else:
            connection.execute("SELECT vault.create_secret(%s, %s)", (value, name))
    connection.execute("SELECT cron.alter_job(jobid, active := true) FROM cron.job WHERE jobname = %s", (JOB_NAME,))


def pause(connection) -> None:
    connection.execute("SELECT cron.alter_job(jobid, active := false) FROM cron.job WHERE jobname = %s", (JOB_NAME,))


def status(connection) -> dict:
    job = connection.execute("SELECT active, schedule FROM cron.job WHERE jobname = %s", (JOB_NAME,)).fetchone()
    ticks = connection.execute("SELECT start_time, status FROM cron.job_run_details WHERE jobid IN (SELECT jobid FROM cron.job WHERE jobname = %s) ORDER BY start_time DESC LIMIT 3", (JOB_NAME,)).fetchall()
    requests = connection.execute("SELECT d.request_id, d.requested_at, r.status_code, r.timed_out, r.error_msg IS NOT NULL, r.content FROM lidl_automation.dispatches d LEFT JOIN net._http_response r ON r.id = d.request_id ORDER BY d.requested_at DESC LIMIT 3").fetchall()
    dispatches = []
    for request_id, requested_at, code, timed_out, error, content in requests:
        item = {"request_id": request_id, "requested_at": requested_at.isoformat(), "http_status": code, "timed_out": bool(timed_out), "response_error": error}
        # Never print the response body or headers. Allow only a numeric run ID.
        try:
            run_id = json.loads(content or "{}").get("workflow_run_id")
            if type(run_id) is int and run_id > 0:
                item["workflow_run_id"] = run_id
        except (ValueError, AttributeError):
            pass
        dispatches.append(item)
    return {"active": bool(job and job[0]), "schedule": job[1] if job else None, "timer_ticks": [{"started_at": started.isoformat(), "status": result} for started, result in ticks], "dispatches": dispatches}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "enable", "pause", "status"))
    action = parser.parse_args().action
    try:
        config = Config.from_env()
        if config.state_backend != "postgres":
            raise ConfigurationError("The cloud timer requires STATE_BACKEND=postgres.")
        token, repository = os.environ.get("DISPATCH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
        if action == "enable":
            validate_dispatch_settings(token, repository)
        store = _Postgres(config.database_url, config.state_account_key, config.database_ca_cert)
        with store._database() as connection:
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '30s'")
                connection.execute("SET LOCAL lock_timeout = '10s'")
                connection.execute("SELECT pg_advisory_xact_lock(1818846316, 1)")
                if action in {"prepare", "enable"}:
                    prepare(connection)
                if action == "enable":
                    enable(connection, token, repository)
                elif action == "pause":
                    pause(connection)
            # The committed state, rather than a transaction's proposed state.
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '20s'")
                report = status(connection)
        print("Cloud timer status: " + json.dumps(report))
        return 0
    except (ConfigurationError, StateStoreError) as error:
        print(error)
    except Exception as error:
        print("Cloud timer setup stopped: " + type(error).__name__ + ". No credentials were logged.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
