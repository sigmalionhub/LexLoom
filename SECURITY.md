# Security policy

## Where to report

Preferred: GitHub's private vulnerability reporting — the **Report a vulnerability** button
under this repository's *Security* tab. It opens a thread only you and the maintainer can
read, it carries the code context with it, and it does not put your address or the finding
in public.

Fallback, if that form is unavailable to you: **sergey.korkishko@sigmalion.io**. Say in the
subject line that the report is a security one.

Do not open a public issue for anything that isn't already public knowledge. There is one
maintainer and no security team, so expect a human reply rather than an acknowledgement bot.

## Scope

- The deployed demo at `https://lexloom.sigmalion.io`.
- This codebase, at any commit on `master`.

Nothing else. There are no other deployments, and no staging environment that mirrors
production traffic.

## What is not in scope here

The project states its own residual risk in [README.md](README.md#honest-limits) and
[README.md](README.md#demo-hardening), and reporting a mechanism that is documented there
as an accepted trade is not a finding. Specifically:

- **The demo-login rate limit is per address, not global.** `enforce_demo_login_rate_limit`
  in [`backend/app/limits.py`](backend/app/limits.py) meters `POST /api/v1/auth/demo-login`
  against the caller's address (or its `/64` for IPv6); a caller spread across many
  addresses is not bounded by it, and that is the stated shape of the limit, not a bypass
  of it.
- **The fixed-window boundary lets an address spend two windows back to back.** The same
  function counts with `INCR` against a key that names the window rather than a sliding
  window, so a burst at a window edge can reach roughly twice the configured limit before
  it is refused. `backend/app/limits.py:enforce_demo_login_rate_limit` documents the
  trade-off against a sliding-window implementation's memory cost; this is that design,
  working as built.
- **IPv6 is bucketed at `/64`.** A visitor delegated a larger block than `/64` (a `/56` or
  `/48`) holds proportionally more rate-limit buckets than an IPv4 visitor. See the
  comment above `_IPV6_BUCKET_BITS` in `backend/app/limits.py` — this is named as a stated
  limitation of the bucketing choice, not an oversight.
- **There is no CORS.** A preflight request returns `405`. The frontend is only usable
  same-origin, behind the reverse proxy — see README.md's "Honest limits" section. Reporting
  the absence of `Access-Control-Allow-Origin` headers is reporting the documented design.
- **`FORCE ROW LEVEL SECURITY` is currently inert.** The migration role that writes tenant
  tables is the bootstrap superuser, which RLS does not bind. This is measured and explained
  in [`docs/adr/0003-tenant-isolation.md`](docs/adr/0003-tenant-isolation.md#2-force-row-level-security-is-inert-in-this-deployment).
  Tenant isolation here rests on every query being scoped through `tenant_session`, not on
  RLS as a backstop; a report that only re-states the ADR's own measurement is not new
  information.
- **Two concurrent streams on the same message both succeed**, leaving two assistant rows.
  The `409` this can trigger is a replay guard, not a lock, and README.md's "Honest limits"
  already says so.
- **A retried `POST` can create a second row.** There is no `Idempotency-Key` anywhere in
  the API, by design, per README.md.

If you find something in this list, you found something the project already knows. What
*is* worth reporting: a way past these limits into something they don't cover — for
instance, a way to read or write another tenant's rows despite `tenant_session` scoping, or
a way to defeat the per-address metering itself (not merely to be outside what it covers).

## No third-party data to protect in the demo

`POST /api/v1/auth/demo-login` provisions a throwaway organisation
(`backend/app/auth/demo.py:provision_demo_org`) with a 24-hour TTL
(`DEMO_ORG_TTL`, `backend/app/auth/demo.py:12`), purged by an hourly job
(`purge_expired_orgs`, same file). Every visitor gets their own tenant and their own copy of
the seed corpus; nobody else's data lives in a demo tenant, because a demo tenant holds
nothing but a copy of a public document (the GDPR text) and whatever the reporter's own
session writes to it. A report that assumes another visitor's private data is reachable
through the demo should first check whether it is instead reachable through the reporter's
own throwaway tenant — which is not a vulnerability, since that tenant was created for the
reporter.

## What not to do

- No denial-of-service testing. `backend/app/limits.py` exists precisely because the
  deployment target is a small shared box where an unbounded process does not degrade
  politely; deliberately exhausting the demo-login limit, the stream ceilings, or the
  ingest cap affects other visitors and possibly other applications on the same host.
- No attempts against the shared host itself — the box, its network, or anything running
  on it besides this application.
- No automated scanning that would consume the demo's capacity: no fuzzers, no scripted
  bulk requests, nothing that treats the 30-tenants-per-hour or 16-concurrent-stream
  ceilings as a target to fill rather than a limit to respect.

Report what you find instead of demonstrating it at scale. A single reproduction is enough.
