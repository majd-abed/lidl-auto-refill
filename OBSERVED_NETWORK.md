# Observed login, allowance, and refill requests

Observed on 9 October 2026 during an authorized temporary inspection of the
customer portal. Credentials were supplied to the inspection process in memory;
no passwords, token/cookie values, browser storage state, screenshots, or full
account responses were saved in the project. The temporary browser contexts
were closed after inspection. The assistant's inspection did not submit a refill.
The user subsequently supplied a HAR containing their own free-refill click.

The HTTP client implements the captured read and refill operations. Mock mode
and dry-run remain the defaults. The client's real mutation has not been run
by the assistant; it still needs a controlled integration test.

## Authentication

- The visible login form accepts a mobile number (`msisdn`) and password.
- The browser submitted `POST https://api.lidl-connect.de/api/token` and received
  HTTP 200.
- Payload field names: `grant_type`, `client_id`, `client_secret`, `username`,
  `password`, `captcha_code`, `captcha_token`. Values were not retained.
- Response field names included `token_type`, `expires_in`, `access_token`,
  `refresh_token`, `mfa_authenticated`, `needs_multifactor_authentication`,
  `risk_of_lockout_due_to_no_alternate_mfa_method`, `mfa_methods`, and
  `X-Multifactor-Token`. Authentication values were not retained.
- API reads sent `Authorization: Bearer <REDACTED>`. The overview loaded and
  returned authenticated query results without a CAPTCHA/SMS prompt during
  this inspection. That does not establish behavior for later logins.
- The portal also posted a hidden form to `/mein-lidl-connect.html`, followed
  redirects, and reached `/mein-lidl-connect/uebersicht.html`.
- Portal cookie names observed: `csrf_contao_csrf_token` and `PHPSESSID`, both
  Secure/HttpOnly and session-only. Session storage key: `portal-session`.
- `x-transaction` appeared on some token/GraphQL requests. Its semantics have
  not been established; it must not be treated as a proven idempotency key.

The login issued a refresh token. Its renewal operation was subsequently
captured and verified as described below; no password login is needed for that
renewal. A future login can still require normal user authentication.

## Session renewal

The website's public frontend defines a Vuex authentication action named
`refresh`. After a normal authorized login in a temporary browser context,
that existing action was invoked once to capture the actual renewal request:

```text
POST https://api.lidl-connect.de/api/token
Content-Type: application/json
x-transaction: <REDACTED>
```

Observed JSON field names: `grant_type`, `client_id`, `client_secret`, and
`refresh_token`. The observed grant value is `refresh_token`; all credential
values remained in memory and were not saved or printed.

The browser renewal returned HTTP 200 with `token_type`, `expires_in`,
`access_token`, and `refresh_token`. `expires_in` was 3600 seconds. Both token
values changed. The exact captured request was then reproduced through ordinary
HTTP using the newest refresh token and the captured transaction header. It
also returned HTTP 200, a one-hour access-token lifetime, and a new token pair,
without browser cookies, a password, or an additional authentication prompt.

This proves the observed renewal flow works in the inspected session; it does
not establish the refresh token's maximum lifetime or whether older tokens
remain usable. No old token was replayed to test invalidation. The application
must persist the newest token pair securely and coordinate renewal across runs
before unattended scheduling. A fixed refresh token in GitHub Secrets alone
cannot retain these newly issued token values. Refresh/session persistence is
not yet implemented in the HTTP client.

## Allowance read

Observed request:

```text
POST https://api.lidl-connect.de/api/graphql
Content-Type: application/json
Authorization: Bearer <REDACTED>
```

Observed JSON envelope: `operationName: "consumptions"`, `variables: {}`, and
the following captured query:

```graphql
query consumptions {
  consumptions {
    consumptionsForUnit {
      consumed
      unit
      formattedUnit
      type
      description
      expirationDate
      left
      max
      tariffOrOptions {
        name
        id
        type
        consumptions {
          consumed
          unit
          formattedUnit
          type
          description
          expirationDate
          left
          max
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}
```

The exact captured read was then repeated using ordinary HTTP and the bearer
token, **without browser session cookies**. Result: HTTP 200, no GraphQL errors,
and numeric allowance data matching the portal's displayed refill allowance.

Response collection: `data.consumptions.consumptionsForUnit` (an array).
Numeric fields: `consumed`, `left`, `max`. Units are explicit strings; observed
data entries used `unit: "GB"` and `formattedUnit: "GB"`.

The account exposed separate `type: "DATA"` and `type: "REFILLABLE_DATA"` entries.
The base bundle was exhausted while the refill bundle still had data. The live
client must handle both; selecting only the first or base entry could trigger
an unnecessary refill. Nested `tariffOrOptions[].consumptions[]` repeat bundle
information and must not be added to the aggregate rows a second time. Validate
expiry, active-bundle membership, and any additional data types before making
a production decision. The implementation rejects unknown data types/units,
expired rows, and duplicate aggregate buckets rather than guessing.

## User-supplied HAR: refill

The HAR contained 39 requests and one refill mutation. Only 20 response bodies
were retained; the pre-refill allowance response body was missing. No original
HAR or full account responses were copied into this project.

Captured request:

```text
POST https://api.lidl-connect.de/api/graphql
Content-Type: application/json
```

```graphql
mutation bookTariffOptionsDirect($bookTariffoptionsDirectInput: BookTariffoptionsDirectInput!) {
  bookTariffoptionsDirect(
    bookTariffoptionsDirectInput: $bookTariffoptionsDirectInput
  ) {
    success
    __typename
  }
}
```

Captured JSON values contain a product identifier, not authentication data:

```json
{
  "operationName": "bookTariffOptionsDirect",
  "variables": {
    "bookTariffoptionsDirectInput": {
      "bookTariffoptions": [{"tariffoptionId": "CCS_92061"}]
    }
  }
}
```

The request returned **HTTP 500** with a GraphQL `errors` array. The user
confirmed that this click was the explicitly free +1 GB action, that the portal
displayed an error, and that the refill was nevertheless applied.

A follow-up `consumptions` request started approximately 45 seconds after the
mutation. It returned an exhausted `DATA` bucket and `REFILLABLE_DATA` with
`left: 1`, `max: 1`, `consumed: 0`, `unit: "GB"`. This records the follow-up
state, not the exact moment the backend updated. The absent pre-refill response
prevents computing an increase from the HAR alone.

The client sends this captured mutation once and verifies allowance even after
an HTTP/network/GraphQL error. It retains a pending record if no increase can
be confirmed; it never treats an error as permission to resend. The product is
fixed to the identifier the user confirmed was free. A live price/eligibility
lookup has not been captured, so revalidate this mapping if the offer changes.

## Application validation

After implementing the captured operations, the Python application ran with
`CLIENT_MODE=http` and `DRY_RUN=true` using a temporary access token supplied
in memory. It retrieved the live allowance successfully and exited with code
0. No refill was submitted, and the temporary token/session was not saved.
The tests include the captured failure pattern: one mutation returns HTTP
500, a subsequent allowance read confirms an increase, and no second mutation
is sent.

The Python session adapter was subsequently validated using ordinary HTTP
password login and renewal, without browser cookies or `x-transaction` headers.
Published client settings were read from the portal bundle at runtime. An
encrypted temporary session was given an expired access-token timestamp to
exercise renewal before a live allowance read; both token values rotated.
A second independent Python process read the same allowance using the persisted
pair without another renewal. Both executions used `DRY_RUN=true`, exited with
code 0, and created zero refill records. Passwords were supplied only in memory;
the temporary encrypted session was removed after validation.

## Still needed

- A programmatic free-offer price/eligibility lookup, if the portal exposes one.
- One controlled refill through this application's HTTP client, with both
  before/after reads, when the threshold is reached and a refill is intended.
- Durable cloud storage for the encrypted session and refill coordination state.
- Any naturally observed expired/revoked-refresh-token behavior.

Follow [DEVTOOLS.md](DEVTOOLS.md). No further HAR is currently required for the
renewal request. Paid top-up operations will not be substituted for the captured
free-refill operation.
