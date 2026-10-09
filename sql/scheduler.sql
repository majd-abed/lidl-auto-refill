-- The timer calls a private function; credentials never appear in cron commands.
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS lidl_automation;
REVOKE ALL ON SCHEMA lidl_automation FROM PUBLIC;

CREATE TABLE IF NOT EXISTS lidl_automation.dispatches (
    request_id BIGINT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE lidl_automation.dispatches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON lidl_automation.dispatches FROM PUBLIC;

-- pg_net temporarily holds the Authorization header until sending the request.
REVOKE SELECT ON net.http_request_queue FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION lidl_automation.dispatch_account_check()
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
    dispatch_token TEXT;
    target_repository TEXT;
    dispatch_request_id BIGINT;
BEGIN
    SELECT decrypted_secret INTO STRICT dispatch_token
    FROM vault.decrypted_secrets WHERE name = 'lidl_github_dispatch_token';
    SELECT decrypted_secret INTO STRICT target_repository
    FROM vault.decrypted_secrets WHERE name = 'lidl_github_repository';
    IF target_repository !~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
       OR dispatch_token NOT LIKE 'github_pat_%' THEN
        RAISE EXCEPTION 'Invalid scheduler configuration';
    END IF;

    dispatch_request_id := net.http_post(
        url := 'https://api.github.com/repos/' || target_repository || '/actions/workflows/account-check.yml/dispatches',
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || dispatch_token,
            'Accept', 'application/vnd.github+json',
            'Content-Type', 'application/json',
            'User-Agent', 'lidl-auto-refill-scheduler',
            'X-GitHub-Api-Version', '2026-03-10'
        ),
        body := '{"ref":"main","inputs":{"scheduled_check":true}}'::jsonb,
        timeout_milliseconds := 10000
    );
    INSERT INTO lidl_automation.dispatches (request_id) VALUES (dispatch_request_id);
    DELETE FROM lidl_automation.dispatches WHERE requested_at < now() - INTERVAL '7 days';
    DELETE FROM cron.job_run_details
    WHERE jobid IN (SELECT jobid FROM cron.job WHERE jobname = 'lidl-account-check')
      AND end_time < now() - INTERVAL '7 days';
    RETURN dispatch_request_id;
END;
$function$;
REVOKE ALL ON FUNCTION lidl_automation.dispatch_account_check() FROM PUBLIC;
