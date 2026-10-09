# Phase 2: capture the actual Lidl requests

The authorized inspection captured login and reproduced the allowance read
over ordinary HTTP. The user's HAR then captured the free-refill mutation and
the user confirmed that it applied despite an error. See
[OBSERVED_NETWORK.md](OBSERVED_NETWORK.md). Token renewal has also been captured
and reproduced through ordinary HTTP. Its endpoint, grant type, and rotation
are recorded in the findings. No additional login/renewal HAR is needed now.
Use requests actually sent by <https://kundenkonto.lidl-connect.de/>.

For GraphQL captures, select **Fetch/XHR** and filter `/api/graphql`. The
allowance request has `operationName: consumptions`; the captured refill has
`operationName: bookTariffOptionsDirect`. Several different
operations share this endpoint, so preserve the `operationName`, full GraphQL
query/mutation text, and redacted `variables` object for each relevant request.
Retain the `DATA` and `REFILLABLE_DATA` allowance rows and their numeric units;
the exhausted base allowance alone does not describe all remaining data.

The HAR already provided the mutation and follow-up allowance, and the renewal
flow was inspected separately. There is no need to trigger another refill or
record another login for these captures. The instructions below remain useful
if the site's behavior changes and fresh observations are needed.

## 1. Prepare Chrome

1. Open the site, press **F12** or **Ctrl+Shift+I**, and choose **Network**.
2. Enable **Preserve log** and **Disable cache** while DevTools is open.
3. Choose **Fetch/XHR** and clear the request list. Keep DevTools open.
4. Log in normally. If login uses a navigation/form redirect and does not appear
   under Fetch/XHR, choose **All** and include the relevant **Doc** requests.
5. Note whether the site requires a password, SMS OTP, CAPTCHA, or a redirect
   to another authentication domain. Complete any challenges normally.

Do not paste a password, OTP, raw Authorization/Cookie/Set-Cookie header,
token-bearing URL, full Copy-as-cURL command, or unredacted HAR. Even a
"sanitized" HAR can retain secrets in request payloads, query strings, and
responses. Share a small manually redacted summary instead.

## 2. Identify the remaining-allowance read

1. Clear the list after logging in. Reload/open the account screen that displays
   the remaining allowance. Write down the visible remaining amount and unit.
2. Inspect new Fetch/XHR requests. In **Preview** or **Response**, find the
   response field corresponding to that visible amount. Check likely account,
   usage, balance, or allowance responses without assuming endpoint names.
3. Record the exact **Headers → General** request URL, HTTP method, response
   status, and content type. Redact phone/account IDs and sensitive query/path
   values while preserving parameter names and the URL's structure.
4. Record the exact JSON field path, type, and unit (for example bytes versus
   MB/GB), whether it is remaining or consumed data, and which entry identifies
   the active data bundle. Include a minimal redacted response retaining field
   names, arrays, types, and nonpersonal numeric allowance values.
5. From **Payload**, record query/body structure if present. From request
   headers, share header **names**, `Content-Type`/`Accept`, and an authentication
   scheme such as `Bearer <REDACTED>` without the actual token.
6. Note cache indicators (`Age`, cache status, conditional requests/304) and
   whether a second normal page refresh fetches a fresh allowance.

If the displayed allowance only arrives inside HTML, note that and identify
the **Doc** response containing it. Do not share the full account HTML.

## 3. Identify the free +1 GB action

1. Open the refill area and inspect its requests first. Capture any response
   that identifies the **free +1 GB** offer, its price, eligibility, exact product
   identifier, and whether a separate paid offer is also present. Preserve the
   nonpersonal free-offer identifier so it can be distinguished reliably.
2. If this only opens a confirmation dialog, inspect the dialog without
   confirming. The final request cannot be inferred from the dialog alone.
3. To capture a mutation, wait until you are eligible and intend to use a free
   refill. Clear Network, then confirm **one** free +1 GB activation. Opening a
   page or clicking a button may itself activate it—follow the actual UI.
4. Capture the request sent at that moment: exact URL/method, query/body,
   content type, response status, and a minimal redacted response.
5. Include request header names and the presence/source of any CSRF token,
   request/transaction identifier, or idempotency key. Share their names and
   locations, with token/key values replaced by `<REDACTED>`.
6. Capture follow-up allowance/status reads. Record the amount before and
   after, how many seconds the update took, and any pending/completed status or
   transaction response. A 200 response by itself is insufficient evidence.

Do not use **Replay XHR**, **Edit and Resend**, or rerun a copied refill cURL
command: these can submit another mutation. If a refill is not currently
appropriate, send the read/authentication captures now; capture the refill later.

## 4. Capture how the authenticated session works

Use **Network → All** for login redirects and **Application → Cookies/Storage**
for session storage. Inspect locally, and send only structural information:

- Login URL/method and field **names**, with every submitted value redacted.
- Redirect hosts and whether an OTP/CAPTCHA is needed.
- Cookie names, domain/path, Secure/HttpOnly flags, expiry or session-only
  behavior; never cookie values.
- Whether reads/mutations send a bearer token, cookies, or both.
- CSRF header/form/cookie names and how the page obtains the token.
- Token storage key names and whether token expiry is reported (do not share
  the token). Record refresh URL/method and payload field names only **if an
  actual refresh request is observed**. Do not invent refresh paths.
- Whether a reload reuses the session; any naturally observed expired-session
  response status/shape, redacted. Do not intentionally lock out the account.

## Summary to send

Complete one block per relevant request using observed values. These are
labels/placeholders, not proposed Lidl endpoint names:

```text
Operation: allowance read / free-offer lookup / refill / login / refresh / verification
Request URL: <observed URL; redact personal IDs and tokens>
HTTP method:
Response status and Content-Type:
Query parameter names and sanitized values:
Request Content-Type:
Request header names:
Authentication: cookies / Bearer <REDACTED> / other observed scheme
CSRF: <header/form/cookie name; value redacted; source>
Request payload: <minimal redacted structure>
Response: <minimal redacted structure; retain allowance fields and unit>
Allowance field path, unit, and active-bundle selector:
Free +1 GB product identifier/price/eligibility, if applicable:
Before/after allowance and update delay, if applicable:
Any observed transaction/status/idempotency behavior:
```

Authentication secrets, if eventually needed for local tests, belong in local
environment variables; deployed secrets belong in GitHub Actions Secrets.
