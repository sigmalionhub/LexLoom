# ADR 0003 — Tenant isolation in the database, and what it does not cover

**Status:** accepted
**Date:** 2026-08-01

## Context

This is a multi-tenant service where the tenants are law firms and the rows are
privileged documents. One cross-tenant read is not a bug report, it is the end of the
product.

The usual approach is a `WHERE org_id = :org` on every query, sometimes wrapped in a
base repository class. It works until the day someone writes one query without it —
and that query looks exactly like every other query in review. The failure mode is
silent: it returns *more* rows, so nothing errors, no test that checks "did I get my
data" fails, and the only signal is a customer seeing another customer's contract.

So the question was where the boundary lives. Not "do we filter", but "what enforces
the filter when a human forgets".

## Decision

**PostgreSQL row-level security, keyed on a transaction-local setting, with an
application role that cannot bypass it.**

Three roles:

```
     rolname      | rolsuper | rolbypassrls
------------------+----------+--------------
 lexloom_app      | f        | f
 lexloom_migrator | t        | t
 lexloom_worker   | f        | f
```

The application and the worker connect as unprivileged roles that own nothing.
Migrations connect as the owner. The tenant identity is set per transaction:

```python
async with session.begin():
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
    )
```

and every org-scoped table carries:

```sql
CREATE POLICY org_isolation ON conversations
  FOR ALL TO lexloom_app, lexloom_worker
  USING      (org_id = nullif(current_setting('app.current_org_id', true), '')::uuid)
  WITH CHECK (org_id = nullif(current_setting('app.current_org_id', true), '')::uuid);
```

Four details that are each a bug if written the other way:

- **`set_config(..., true)`, not `SET`.** The third argument makes the setting
  transaction-local. A session-scoped `SET` survives the connection's return to the
  pool and the *next request* inherits the previous tenant's id. `SET LOCAL` cannot
  take bind parameters, which is why people interpolate the value — injecting
  attacker-influenced text straight into the security control. `set_config`
  parameterises.
- **In the session dependency, not in middleware.** Middleware and route handlers
  check out different pooled connections, so a setting applied in middleware is absent
  where it matters.
- **`WITH CHECK` written explicitly** even though `FOR ALL USING` implies it. Without
  it, org A can `INSERT` rows stamped with org B's id.
- **`nullif(...)` rather than `current_setting(...) IS NULL OR org_id = ...`.** The
  second form is what people reach for to make the worker and the migrations work, and
  it is a *complete bypass* for anything that reaches the database without setting the
  variable. Absence of context must mean **no rows**, never **all rows**. A bypass is
  expressed as a role, never as absent state.

A SQLAlchemy `after_begin` listener refuses to let a session begin without an
organisation, so forgetting the context is a crash rather than a leak. System paths
opt out explicitly and are greppable. And `assert_unprivileged_role` runs at API and
worker start-up, refusing to boot if the connected role is a superuser, has
`BYPASSRLS`, owns a table in `public`, or is a *member* of a role that is any of
those.

### What it looks like from a tenant's connection

Org B, connected as `lexloom_app` with its own context set, holding org A's ids:

```
 my_chunks                  |  30
 all_conversations_visible  |   0     -- unfiltered SELECT, org B has no conversations
 all_orgs_visible           |   1     -- its own row, and only its own
 a_conversations            |   0     -- explicitly asking for org A's rows
 a_chunks                   |   0

INSERT INTO conversations (…, org_id, …) VALUES (…, '<org A>', 'stolen');
ERROR:  new row violates row-level security policy for table "conversations"

UPDATE conversations SET title = 'seized' WHERE org_id = '<org A>';   -> UPDATE 0
DELETE FROM conversations           WHERE org_id = '<org A>';         -> DELETE 0
DELETE FROM organizations           WHERE id     = '<org A>';         -> DELETE 0
```

With no tenant context set at all, every org-scoped table returns zero rows and no
error:

```
 organizations   | 0
 conversations   | 0
 messages        | 0
 document_chunks | 0
```

This is what makes the API's 404 discipline free rather than hand-maintained: the
handler never holds another tenant's row, so it cannot leak one by accident. See
[api-contract.md §7](../api-contract.md#7-what-a-cross-tenant-request-looks-like).

---

## What this covers, and what it does not

The point of this table is the third column.

| Control | What it covers | **What it does not cover** | Compensation |
|---|---|---|---|
| `org_isolation` policies on `conversations`, `messages`, `document_chunks` | Every `SELECT`/`INSERT`/`UPDATE`/`DELETE` these two roles issue, including one with a forgotten `WHERE`. Verified by attack, not by reading. | Anything connecting as a different role. Any query issued by the migration role. | The start-up guard refuses to boot on a privileged role, so the application cannot silently become the exception. |
| The `app.current_org_id` setting | The tenant identity for the transaction, parameterised so it cannot be injected into. | **Any role can set it to any value.** It is a variable, not a capability. RLS here defends against a forgotten `WHERE`, not against an attacker who has achieved SQL execution. | The value is written in exactly one place, from a claim in a verified JWT. There is no code path that takes it from a header, a query parameter or a body. |
| `FORCE ROW LEVEL SECURITY` on all four tables | Would make the policies bind the *table owner* as well. | **Nothing, in this deployment.** See below — it is currently inert. | Kept because it binds the day the migration role stops being a superuser, which is the only shape in which the owner's isolation claim is honest. |
| `org_self_insert` on `organizations` | Nothing. It is `WITH CHECK (true)` by design. | **Any limit on organisation creation, and any constraint on what an inserted row says.** See below. | The limit is an endpoint concern, and it is enforced there: `POST /auth/demo-login` is metered per client address in `backend/app/limits.py`. Per address, not globally. |
| The schema's parent→child relationships | Nothing. **There are no foreign keys at all.** See below. | Referential integrity, cascade deletes, orphan prevention. | Every parent lookup is an explicit `SELECT` inside the tenant's context; the purge deletes children by identity. Both are convention. |
| PgBouncer / a transaction-mode pooler | Not in use today. `set_config(..., true)` inside an explicit transaction is safe under transaction pooling, because the server connection is pinned for the transaction's duration. | Session-scoped `SET` would **not** be safe. `LISTEN`/`NOTIFY` and session advisory locks are unavailable under transaction pooling. | The transaction-local form is the only one used, and this row exists so that nobody "optimises" it into a session `SET` later. |
| The worker role (`lexloom_worker`) | Runs under the same policies as the API; `org_id` travels in the task payload; the same start-up guard runs post-fork in every prefork child. | The task *payload* is not itself validated as belonging to the caller — a task enqueued with the wrong `org_id` acts as that tenant. | Payloads are constructed server-side from a verified principal, never from client input. This is a convention, and it is the one most likely to be broken by a future feature. |
| Redis / Celery result backend keys | Nothing. Keys are not tenant-scoped. | A task id addresses a result regardless of who asks. | Task ids are never returned to clients, so there is nothing to address. Again a convention, not a control. |
| Object storage (M2) | Not built yet. | Bucket keys are outside the database and outside RLS entirely. | Planned: originals deleted after parsing, so the retention window is minutes; keys derived from ids that RLS already gates. Recorded here so the gap is not discovered later. |
| Logs | Structured JSON with a `request_id`; tracebacks are logged as **strings**. | Anything deliberately logged is outside every database control. | Frame-local logging was tried and removed: it copies every frame's locals, and in an ingestion path those hold tenant document text. Rejection reasons go to the log and never to the caller — the log is the privileged side of that boundary, so it must not become an export. |
| Embeddings (M2) | Not built yet. `document_chunks.embedding` is RLS-covered like any other column. | A vector is invertible to substantial source text. An index dump or a `/search` response returning raw vectors is equivalent to a document dump. | Planned: vectors never leave the API and never appear in the OpenAPI schema. Stated now so the constraint exists before the feature does. |
| `purge_expired_orgs()` (`SECURITY DEFINER`) | Cross-tenant deletion of expired demo tenants, which no ordinary role can do. | Everything else — it takes no arguments and its predicate is hardcoded. | Owned by `lexloom_migrator`, `EXECUTE` granted to `lexloom_worker` only, returns `integer`. See the metadata below. |

The definer function, read from the catalog:

```
      proname       |      owner       | prosecdef |                    proconfig                        | returns
 purge_expired_orgs | lexloom_migrator | t         | {"search_path=pg_catalog, public",row_security=off}  | integer
 proacl: {lexloom_migrator=X/…, lexloom_worker=X/…}
```

It **returns an integer, deliberately**. An integer is not a credential.

---

## Three things a reader cannot get from the code

### 1. This schema has no foreign keys. None.

```sql
SELECT contype, count(*) FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'public' GROUP BY contype;

 contype | count
---------+-------
 p       |     5     -- primary keys
 u       |     1     -- one unique constraint
```

No `f`. `messages.conversation_id` does not reference `conversations.id`.
`document_chunks.org_id` does not reference `organizations.id`. Nothing does.

**This is deliberate.** A foreign-key check runs as a system-internal operation that
is not subject to row-level security. So a constraint spanning a tenant boundary
answers a question the querying tenant is not allowed to ask: insert a child row
pointing at a parent id and the presence or absence of a constraint violation tells
you whether that parent exists, in *somebody's* tenant. That is an existence oracle
built into the schema, on by default, and impossible to filter.

**The cost is real and has already been paid.** The database does not maintain the
parent→child graph; convention does. Concretely:

- Deleting an `organizations` row does not cascade. The first version of the demo
  purge deleted only the organisation, which would have left that tenant's
  conversations, messages and chunks in the database **permanently** — invisible to
  every tenant, because RLS keys on an org id nobody holds any more, and unreachable
  by a later purge, because the purge only scans `organizations`. The endpoint's
  entire promise is that a demo tenant's data goes away. It would not have. The purge
  now deletes children explicitly, keyed on the organisation ids that same run just
  destroyed.
- A child row can be created pointing at a conversation that does not exist. Nothing
  in the database prevents it, so `POST …/messages` does an explicit existence
  `SELECT` inside the caller's tenant context. That lookup is not defence against
  cross-tenant writes — `org_id` is always the caller's own — it is defence against
  unreachable rows in a tenant nobody can list.
- A safety argument that says "a child is only ever created after its organisation"
  is a statement about ordering, and ordering is not enforced here. That claim was
  written in a migration comment, and it was false: an organisation insert that has
  not committed yet, with a child committed by another transaction, breaks it.

Every future feature that adds a table will meet this again. The trade is worth it,
and it must be stated rather than discovered.

### 2. `FORCE ROW LEVEL SECURITY` is inert in this deployment

All four tables carry it:

```
     relname     | relrowsecurity | relforcerowsecurity
-----------------+----------------+---------------------
 conversations   | t              | t
 document_chunks | t              | t
 messages        | t              | t
 organizations   | t              | t
```

`FORCE` exists to make a table's policies bind its **owner**, who otherwise bypasses
them. But in this stack the owner is `lexloom_migrator`, which is the Compose
bootstrap superuser — and **superuser bypass is unconditional and outranks `FORCE`**:

```
   current_user   | rolsuper | rolbypassrls
------------------+----------+--------------
 lexloom_migrator | t        | t

SELECT count(*) FROM organizations;   -->  1605   (a development database, 2026-08-01)
```

Every organisation, of every kind — the permanent seed corpus, the throwaway tenants a
demo login creates, and everything the test suite has left behind. (ADR 0001 quotes the
TTL-bearing subset of the same table, 129 of them, for a different argument; the numbers
differ because the populations do.) Both move with every demo login and every test run,
so reproduce them rather than compare them.

The property is the invariant: **an unscoped `SELECT` from the owner returns every
tenant's rows, on tables that all carry `FORCE`.** The same query as `lexloom_app`
returns `0`. So on this deployment `FORCE` protects nothing.

It stays, and the reason is worth being precise about: it is the flag that makes the
policies bind **the day the migration role stops being a superuser**, which is the
only deployment shape in which "the database enforces isolation" is true without
qualification. Keeping it costs nothing and removing it would silently remove the
protection at exactly the moment someone improves the deployment.

Two consequences follow from saying this plainly rather than letting the flag imply
otherwise. The first is that a test asserting `relforcerowsecurity` is asserting a
property that currently has no effect — worth keeping, worth labelling. The second is
harder: a `SECURITY DEFINER` function owned by a *non*-superuser owner of a `FORCE`d
table has no policy granting the owner anything, so it would silently delete nothing.
The purge would stop working the day someone follows this document's own advice. That
is handled rather than documented: the function carries `SET row_security = off`,
which turns the silent no-op into an error naming the fix, and has no effect under
today's superuser owner.

### 3. `org_self_insert` is `WITH CHECK (true)`

```
   tablename   |   policyname    |  cmd   | qual                                   | with_check
 organizations | org_self_insert | INSERT |                                        | true
 organizations | org_self_select | SELECT | (id = …current_setting…)               |
 organizations | org_self_update | UPDATE | (id = …current_setting…)               | (id = …current_setting…)
 organizations | org_self_delete | DELETE | (id = …current_setting…)               |
```

It has to be. A first-time visitor has no organisation, therefore no tenant context,
therefore nothing for a policy to scope an insert to. Demo login provisions the
organisation *before* the visitor has an identity, so the insert policy cannot be
conditioned on one.

The consequence, stated so nobody has to infer it: **any session on an application
role, with or without tenant context, can insert an `organizations` row with an
arbitrary id.** Verified:

```sql
BEGIN;
SELECT current_setting('app.current_org_id', true) IS NULL;   -- t: no context at all
INSERT INTO organizations (id, external_id, name)
VALUES ('99999999-9999-4999-8999-999999999999', 'probe', 'made by a visitor with no context');
-- INSERT 0 1
SELECT count(*) FROM organizations;   -- 0: it cannot even see the row it just wrote
ROLLBACK;
```

So **the limit on organisation creation is a rate limit on an endpoint, and not a
property of the policy.** `POST /api/v1/auth/demo-login` is unauthenticated and each
call writes an organisation plus a 30-row corpus copy at about 22 ms per call, so the
endpoint is where the ceiling has to live — and it does: `backend/app/limits.py` meters
tenant creation per client address, 30 an hour by default, refusing with `429` and a
`Retry-After`. Concurrent streams are capped in the same module. The numbers and their
sizing are in [errors.md](../errors.md#admission-control).

What that closes is one address exhausting the box. What it does not close, and what
this policy therefore still leaves open:

- **The meter is per address.** A caller spread across many addresses is unbounded by
  anything here; the 24-hour TTL is what reclaims the rows.
- **The id is the caller's choice.** Shown above.
- **So is `expires_at`.** `WITH CHECK (true)` checks no column, so a session on an
  application role can write an organisation with a TTL of its own choosing — or with
  `NULL`, which is how the permanent seed organisation marks itself and which
  `purge_expired_orgs()` reads as "never collect this". A row inserted that way is
  invisible to its creator and to every tenant, and permanent. Nothing in the API
  offers that: `provision_demo_org` computes `expires_at` server-side from
  `DEMO_ORG_TTL`, and there is no code path that takes it from a request. It is a
  property of the *policy*, and it is here because the same sentence that makes the id
  free makes every other column free with it.

Read/update/delete are still confined — the row is invisible to its own creator until a
context names it — so all of this is an availability and storage exposure, not a
confidentiality one.

One related Postgres behaviour, since it is the trap this policy set creates.
`INSERT … ON CONFLICT` with an explicit conflict target against `organizations`
**always** errors, even with no real conflict, because the conflict path evaluates the
`SELECT` policy's qual — which differs from the `INSERT` policy's `WITH CHECK`:

```sql
INSERT INTO organizations (id, external_id, name)
VALUES (gen_random_uuid(), 'brand_new_never_seen', 'probe')
ON CONFLICT (external_id) DO NOTHING;
ERROR:  new row violates row-level security policy for table "organizations"
```

The same statement without `ON CONFLICT` succeeds. And the *untargeted*
`ON CONFLICT DO NOTHING` does not error — it silently no-ops against a colliding row
in **any** tenant, which is an existence oracle for other organisations' external ids.
Neither form is acceptable here, so nothing that writes this table uses one.

The two writers handle the collision they cannot use `ON CONFLICT` for differently, and
the difference is not cosmetic:

- `provision_demo_org` (`app/auth/demo.py`) issues a plain `INSERT` and catches nothing.
  Its `external_id` is derived from a fresh uuid4, so a unique violation is not a case
  that arises; a `try` there would be an unreachable branch pretending to be a guard.
- `seed_public_corpus` (`app/chat/corpus.py`) inserts a row with a fixed id and does
  catch `IntegrityError`, because on every run after the first that row is already
  there. That one is wrapped in a **savepoint**, and the savepoint is load-bearing: a
  unique violation aborts the enclosing transaction, so swallowing the exception without
  one leaves the session poisoned and the real failure surfaces at commit, pointing at
  the wrong line entirely.

---

## Consequences

- Isolation survives a forgotten `WHERE`. That is the whole point, and it is the one
  property most likely to be needed.
- `search_chunks` carries **no** `org_id` clause at all. Two tests hold that: one shows
  another tenant's rows do not come back, the other shows the statement that reached
  Postgres never asked them not to. So what excluded them is the policy.
- Every unscoped read is a bug. `system_session()` returns zero rows from all four
  tables — a trap that catches whoever is not paying attention, because it fails by
  returning nothing rather than by erroring.
- Migrations are rewritten in place before first deploy, and never after. A reader of
  this repository should find four coherent migrations telling the schema's story, not
  "0004 creates a dangerous function, 0005 removes it". Git history carries the
  correction for anyone who wants it. After first deploy that stops being available and
  the rule inverts.

---

## What I got wrong and what it cost

### A table everyone believed was protected, and was not

`organizations` shipped with **no row-level security at all** and both application
roles holding full DML on it. From the first migration until it was caught, and
through a project whose headline claim is database-enforced isolation.

It was not found by reading the policies. It was found by connecting as `lexloom_app`
with one tenant's context set and trying the things that were supposed to fail:

```
SELECT  -> 49 other organisations returned
UPDATE  -> 49 rows touched
DELETE  -> 49 rows touched
```

(Probes rolled back.) Every other table was correct. Every test was green, because
every behavioural test touched `document_chunks`, and the catalog test asserted two
booleans over a hardcoded table list — so a table with no policy at all was simply
not on the list.

The cost was not the fix; the fix is migration 0003 and it is twenty lines. The cost
is what it says about the evidence. This is the strongest lesson in the project: **a
security property is a claim about behaviour, and reading the code that implements it
is not evidence.** Connecting as the unprivileged role and attempting the forbidden
operation is. Every isolation claim in this document above was re-derived that way,
which is why the transcripts are pasted rather than described.

### Guards that reported success against the thing they existed to catch

This happened five times, and each time the artefact that failed was the one built to
close the previous failure. Naming them in order, because the pattern is the finding:

1. A test asserting the JWT signing keypair was ephemeral compared only the `kid` —
   which is a `uuid4` generated independently of the key. A memoised keypair would
   have kept it green. Now it compares the RSA modulus.
2. A test pinning a `SECURITY DEFINER` function's `search_path` asserted only that
   *some* `search_path=` entry existed, with no opinion on the value. Both
   `pg_catalog, public` and `attacker_controlled_schema` passed. The sabotage used to
   "prove" the test worked was `RESET search_path`, which strips the setting entirely —
   the one mutation the assertion happened to catch. **The sabotage did not correspond
   to what the test asserted.** With the order reversed and a hostile `public.now()`
   returning `infinity`, the purge selected a tenant expiring in a hundred years and
   the guard said `1 passed`.
3. The guard built to stop a definer function handing out organisation ids asserted
   `"uuid" not in <return type>`. A function `RETURNS SETOF text` selecting `id::text`
   gave identical capability and the test stayed green.
4. Rewritten as a privilege check, it was blind to `GRANT … WITH INHERIT FALSE,
   SET TRUE` (privilege functions answer "right now", and `SET ROLE` is not now), to
   `EXECUTE` being checked at `CREATE TRIGGER` time rather than at fire time, and to a
   column-level `GRANT SELECT (id)` — because `has_table_privilege` is false unless the
   privilege covers the whole table, so a view exposing only ids became *invisible* to
   the guard.
5. Rewritten as an inventory, it was blind to role grants — because an inventory
   enumerates objects, and **a role grant creates no object**. `GRANT lexloom_migrator
   TO lexloom_app WITH INHERIT FALSE, SET TRUE` gave full enumeration via `SET ROLE`
   with the suite green and the start-up guard reporting `rolsuper=f rolbypassrls=f
   owned=0`.

What finally converged was a change of shape, not another patch. **A guard that
enumerates ways to reach the data can never converge, because the space of routes is
unbounded and Postgres privilege semantics has a corner in every direction. A guard
that inventories what exists converges, because the thing asserted is finite and the
assertion needs no privilege semantics at all.** The invariants now assert that the
set of user-created relations *is* this literal list, the set of user-created
functions *is* this one entry, the set of role memberships for both application roles
*is* empty, and the application's route surface *is* this list. `oid >= 16384` is the
discriminator, and it is independent of schema, owner and grants — which is exactly
where all the bypasses lived.

Two costs. The direct one: five successive rewrites of a guard, while the production
policies underneath it had been correct and byte-identical since the first one. The
one that matters more: for most of that time the repository held a green suite
asserting a property it did not check — worse than having no test, because a green
test gets *quoted* as evidence. The start-up guard now checks role membership as well,
because the cheap second line belongs in the process that actually runs and not only
in the suite.

### A helper that returned the isolation credential while calling it opaque

The demo purge needs to cross tenant boundaries, which no ordinary role may do. The
first mechanism was a `SECURITY DEFINER` function `expired_org_ids()` returning the
ids of expired organisations, with a comment describing them as opaque identifiers.

**An organisation id is not opaque. It is the credential.** RLS keys on it, so
possessing one *is* the authority to act as that tenant. From an ordinary
`lexloom_app` session with no prior knowledge:

```sql
SELECT set_config('app.current_org_id', (SELECT * FROM expired_org_ids() LIMIT 1)::text, true);
-- then: read that tenant's name, its conversation, its message, its document chunk,
-- and DELETE FROM messages (rolled back)
```

Worse, it *created* the exposure rather than widening one. Before that function
existed, an unscoped `SELECT count(*) FROM organizations` returned `0` for the
application role, and even `pg_stats` was suppressed by RLS, so guessing a random
UUIDv4 was the only route in. The function removed the barrier. And the test named
`test_an_expired_organisation_stays_invisible_to_other_tenants` stayed green through
the entire escalation, because it probes the policy route from inside a tenant session
while the leak was on the unscoped route the function opened.

The trade had also been chosen backwards. The alternative — bounded `SELECT`/`DELETE`
policies on `organizations` for expired rows — was rejected because it would leak the
*name* of an expired tenant. The mechanism chosen instead leaked the key that unlocks
all four tables for those same tenants: a strict superset of the leak it was picked to
avoid, while the migration comment asserted the opposite.

The fix was to remove the capability rather than fence it. `expired_org_ids()` is gone;
`purge_expired_orgs()` does the work and returns a count. Narrowing the grant to the
worker role alone was considered and rejected as insufficient — it keeps the
enumeration primitive and merely changes which credential unlocks it.

### Two Postgres semantics where the idiomatic form is the broken one

Both were found by experiment after a statement reported success and did nothing.

**`DELETE … RETURNING` silently filters the delete.** Adding `RETURNING` pulls the
`SELECT` policy into the statement, so rows the `DELETE` policy permits but the
`SELECT` policy hides are not deleted — and nothing raises. Isolated on a table with a
deliberately broader `DELETE` policy than `SELECT` policy, the only difference between
the two statements being `RETURNING`:

```sql
DELETE FROM probe;              -- DELETE 2
DELETE FROM probe RETURNING id; -- DELETE 1
```

**`ON CONFLICT` with an explicit target raises unconditionally** against
`organizations`, shown earlier. Both have the same shape: the form a competent
engineer reaches for first is the broken one, both fail quietly or fail in a way that
misattributes the cause, and neither is discoverable by reading. Any future
cross-scope `DELETE … RETURNING` in this codebase will no-op silently.

### Retrieval that found nothing and raised nothing, twice

Not an isolation defect, but the same failure class, and it is the one a client would
have seen.

The demo showed empty citations for two entirely different reasons in succession.
First, `provision_demo_org` created the organisation and copied no corpus, so a demo
visitor owned zero chunks: retrieval returned `[]`, the `citations` event arrived
empty, and the headline behaviour — sources rendering before the first token — quietly
did not happen. Nothing raised.

Fixed by copying the corpus per tenant, in the same transaction as the organisation
row. **Not** by giving a shared corpus organisation a read-through policy exception —
that would punch a hole in the exact thesis the project exists to demonstrate, and
every isolation transcript in this document would then be describing a system with a
deliberate exception in it.

Then, with a corpus present, retrieval was a literal `ILIKE '%…%'` substring match. A
natural-language question is not a substring of any chunk, so `"What are the
processor's obligations?"` matched **zero** rows against a corpus that plainly
contains Article 28(3). Also silent. Three shapes were measured before replacing it,
rather than reasoned about: `ILIKE` → 0; `plainto_tsquery` → 0 as well, because it
`AND`s every content word and no single chunk holds them all; an OR-of-lexemes
`tsquery` ranked by `ts_rank` → the right articles.

And the first full-text implementation carried the same bug a third time: it built the
query with `to_tsquery`, which runs the dictionary — so it stemmed lexemes that
`to_tsvector` had **already** stemmed. Twenty-one of the corpus's 276 distinct lexemes
are altered by that second pass, and the words that break are ordinary ones:

| question word | lexeme | stemmed again | hits via `to_tsquery` | hits via the cast |
|---|---|---|---|---|
| `responsibility` | `respons` | `respon` | **0** | 68 |
| `infringement` | `infring` | `infr` | **0** | 68 |
| `authorisation` | `authoris` | `authori` | **0** | 68 |
| `guarantee` | `guarante` | `guarant` | **0** | 34 |
| `pseudonymisation` | `pseudonymis` | `pseudonymi` | **0** | 34 |
| `representative` | `repres` | `repr` | **0** | 34 |
| `erasure` | `erasur` | `erasur` | 34 | 34 |

The last row is the reason it survived a first look: some words are unaffected, so
spot-checking finds a working demo. Finds nothing, raises nothing — the same shape as
the bug it replaced. The assembled string is now **cast** to `tsquery` rather than
passed through `to_tsquery`, with explicit quote-and-backslash escaping (a quoted
tsquery lexeme doubles both the quote and the backslash, while `quote_literal` switches
to `E'…'` on a backslash, which is not tsquery syntax), and pinned by a test named for
the property.

The through-line with everything above: **the failures that survive review are the
ones that return a plausible answer.** A leak returns more rows than it should. A
blind guard returns `1 passed`. A broken search returns `[]`. None of them raises, and
none of them is visible in a diff.
