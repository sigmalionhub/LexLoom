# Deploying LexLoom

The target is a **shared production host**, not a sandbox. It already serves other
applications behind the same nginx, has **2.2 GB of free RAM**, 2 vCPUs, and **arrives
with no swap** — Step 2 adds 2 GB of it, and that is the single most valuable line in
this file. Read the sizing below as the argument for why: it is written for the box as
found, and it does not stop applying once the swapfile is on.

With no swap at all, a memory spike has exactly one outcome: the kernel OOM killer picks
a victim by its own scoring, and the victim may be a *neighbouring* application rather
than this one. Swap removes that as the *first* thing that happens — the box thrashes
instead, which is slow rather than fatal, and slow is recoverable. It does not remove it
as the last thing that happens: 2 GB of swap on a 2 vCPU box is a delay, not a budget,
and an unbounded process still gets there.

So the swapfile and the ceilings do different jobs and both are needed. Every memory
limit and every ceiling in this stack exists because of that, not because of tidiness.

Read this whole file before running anything. Roughly ten minutes, most of it waiting.

---

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Postgres 16 + pgvector | container | `127.0.0.1:5433` only, never published publicly |
| Redis | container | published on `127.0.0.1:6379`, and **`requirepass` is what protects it** — see below |
| API (FastAPI) | container | `127.0.0.1:8100` only |
| Worker (Celery) | container | no ports |
| Beat (Celery beat) | container | no ports — fires `purge_expired_orgs` once an hour (`PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS`); that one task enforces the demo-organisation TTL. It is no longer what frees an `INGEST_ORG_CAP` slot: the count requires the organisation to be live (migration 0008), so a slot comes back when the TTL lapses rather than up to an hour later. The purge still reclaims the rows |
| Frontend | **files on disk** | statically exported, served by nginx — no Node process |
| nginx | host, already installed | terminates TLS, serves the files, proxies the API |

The frontend and the API share one origin. That is a requirement, not a preference: the
backend ships **no CORS headers at all**, so a frontend on a second origin could not call
it.

**"Bound to 127.0.0.1" is not a security property on this box, and reading it as one is how
the Redis instance came to be unauthenticated.** This host is shared: every process running
under any local account reaches host loopback, which is precisely why `TRUSTED_PROXY_SECRET`
exists at all — the API is also on `127.0.0.1` and that is exactly not enough. Loopback
keeps a service off the *internet*; it keeps it away from nobody on the machine.

So each published port needs its own answer to "what stops a neighbouring process?", and
each has one:

- **Postgres `5433`** — a password per role, and the roles are unprivileged (`app/db/engine.py`
  refuses to start as one that can bypass RLS).
- **Redis `6379`** — `requirepass`, set from `REDIS_PASSWORD` in Step 3. Without it, any local
  account could run `KEYS lexloom:*` and read live organisation ids out of
  `lexloom:ingest:{org_id}:{source_key}` and `lexloom:limits:streams:org:{org_id}` — and an organisation id
  *is* the credential every row-level-security policy in this schema keys on — then queue
  `lexloom.ingest_source` for one of them behind the admission check's back, or `FLUSHALL`
  every rate-limit counter. The API, worker and beat reach Redis over the Compose network,
  never through this port; it is published only so `pytest` can reach it from the host.
- **API `8100`** — a bearer token per request, plus `TRUSTED_PROXY_SECRET` so a forwarded
  address is believed only when it came through this nginx.

---

## Step 1 — DNS (do this first; certificate issuance depends on it)

Add an **A record** in the `sigmalion.io` zone (DNS is hosted at Spaceship —
`launch1.spaceship.net`, `launch2.spaceship.net`):

```
lexloom.sigmalion.io.   A   46.225.217.79
```

Verify before continuing — a certificate request against a name that does not resolve
fails in a way that is tedious to unpick. Ask the **authoritative** servers, not your local
resolver, which can hold a cached negative answer from before you added the record:

```bash
NS=$(dig +short NS sigmalion.io | head -1)

dig +short @"$NS" lexloom.sigmalion.io A     # must print 46.225.217.79
dig +short @"$NS" zzq7x-nope.sigmalion.io A  # must print NOTHING
```

The second line is not padding. If a random subdomain also resolves, the zone has a
**wildcard**, the first line proves nothing about your record, and the certificate will be
issued for a name that points wherever the wildcard points. A wildcard answers every name,
so querying `*.sigmalion.io` literally does not detect one — only a name nobody would ever
create does.

Repeat a failing query two or three times before concluding anything. A one-off `SERVFAIL`
is a hiccup; a consistent `SERVFAIL` — as opposed to `NXDOMAIN` — means something *does*
exist at that name and is broken, usually a stale delegation, and adding an A record at the
parent will not fix it until that is removed.

## Step 2 — host preparation (requires sudo, so a human runs it)

```bash
# Let the deploy user drive Docker without sudo.
sudo usermod -aG docker deploy
# Group membership is read at login. Either log out and back in, or:
newgrp docker

# 2 GB of swap. Without it, a memory spike in the demo can get one of the OTHER
# applications on this box OOM-killed instead of the demo — the kernel picks by
# its own scoring, not by who caused it. With it, the first symptom is a slow
# box rather than a dead neighbour. It buys time, not headroom: the container
# limits below are still what bounds the total.
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Confirm
free -h        # Swap: 2.0Gi
docker ps      # must work without sudo
```

## Step 3 — get the code and build

```bash
git clone git@github.com:sigmalionhub/LexLoom.git /var/www/portfolio/lexloom/app
cd /var/www/portfolio/lexloom/app
cp .env.example .env
```

Edit `.env` before starting anything:

- **set `ENV=demo`** — not `local`, and not `production`. `demo` is a real value, not a
  label: it makes the demo-login rate limit and the stream ceilings **mandatory** (the
  container refuses to start with `LIMITS_ENABLED=false`), requires the JWKS URL to be
  HTTPS, and requires the proxy secret below — while still permitting the development
  identity provider, which refuses to start under `production`. Left at `local`, every
  one of those checks is inert and the box is unprotected while looking configured.
- set `MIGRATOR_DB_PASSWORD`, `APP_DB_PASSWORD` and `WORKER_DB_PASSWORD` — all three, not
  just the two the application uses. **Generate them as hex**, with the same command as
  the proxy secret below:

  ```bash
  openssl rand -hex 32     # once per password
  ```

  Not `base64`, and not a passphrase. Each of these values is interpolated into a URL
  (`DATABASE_URL` in `docker-compose.yml`, and the `export MIG=` line further down),
  where `@` and `/` are structural and silently cut the DSN in the wrong place — the
  failure is "password authentication failed" for a user you did not name. Hex has no
  character that means anything in a URL, in shell, or in SQL.
- **set `REDIS_PASSWORD`** to a fresh value — the same `openssl rand -hex 32`. The line in
  `.env.example` is committed in this repository and therefore is not a secret; leaving it
  is the same class of mistake as leaving the proxy secret's placeholder, and it fails the
  same way: `app/redis_auth.py` refuses to start the API, the worker and beat under
  `ENV=demo` while that value is in use, so `docker compose ps` shows containers that never
  reach `Up` rather than a stack that looks healthy and takes anonymous connections. Compose
  interpolates the value into `--requirepass` *and* into every service's `REDIS_URL`, so
  there is nothing to keep in step by hand. Changing it later is safe and takes effect on
  the next `docker compose up -d` — unlike the database passwords below, Redis holds no
  volume that remembers the old one.
- set `ERROR_TYPE_BASE_URL=https://lexloom.sigmalion.io`
- set `AUTH_JWKS_URL=https://lexloom.sigmalion.io/.well-known/jwks.json` — the API
  verifies its own tokens against this, and `ENV=demo` refuses a plaintext URL because a
  spoofed key set would make every subsequent token verify
- set `TRUSTED_PROXY_SECRET` to a fresh value and put **the same string** in the nginx
  snippet in step 5:

  ```bash
  openssl rand -hex 32     # keep this; it goes in two places
  ```

  Without it, anything on this shared box that can reach `127.0.0.1:8100` — including a
  server-side request forgery in a neighbouring application — can forge
  `X-Forwarded-For` and create demo tenants without limit. The container refuses to
  start without it under `ENV=demo`.
- optionally set `GEMINI_API_KEY` to a Google AI Studio key if this deployment should
  offer a real generator alongside the deterministic one. **Empty is a working state,
  not a misconfiguration**: with no key, `GET /api/v1/generators` lists only
  `deterministic`, so the frontend never offers a choice the box cannot honour, and a
  request that reaches the stream endpoint for a Gemini choice anyway gets
  `422 generator_unavailable` rather than a raw `500` (`backend/app/api/v1/generators.py`).
  The key is forwarded to the `api` service only — the worker and beat never generate an
  answer, so `docker inspect` on either would otherwise show it to anyone in the `docker`
  group on this shared box (`docker-compose.yml`). It is sent as the `x-goog-api-key`
  header, never a query parameter, so it never lands in an nginx access log, a proxy in
  front of this box, or anyone's shell history (`backend/app/chat/gemini.py`) — put it in
  `.env` the same way as every other secret in this list and nowhere else; `.env` is
  `.gitignore`d, and pasting the key into a command line or a committed file defeats the
  point of the header.
- optionally set `INGEST_ORG_CAP` (default `8`) to change how many organisations may
  hold an ingested corpus at once — see the comment beside it in `.env.example` for the
  memory arithmetic behind the default. Note what it does **not** bound: it is a global
  ceiling, so one address filling it denies the ingest feature to every later visitor
  until those tenants expire (two hours after they started an ingest, not the usual
  twenty-four). That is a known limitation, written up in `backend/app/ingest/jobs.py`;
  the demo-login rate limit is what actually bounds how fast anyone can fill it.

The passwords are read **only when the data volume is first created**. Changing them later
edits nothing inside a Postgres that already exists, so set them now.

```bash
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

`GIT_SHA` is not a line in `.env` — a value written there once is correct once and lies at
every build after it. Computed on the command line it cannot drift from what is actually
being built, and it is the only fact `GET /api/v1/status` reports about the deployment
(see "Metrics" below). Left unset it builds as `unknown`, which is the honest answer for a
container not built through this runbook.

Then apply migrations. **The API container cannot do this**, by design — it connects as an
unprivileged role that owns nothing, which is what makes the tenant isolation claim
enforceable. Migrations run as the migration role, so both commands below carry its DSN:

```bash
export MIG="postgresql+asyncpg://lexloom_migrator:$(grep '^MIGRATOR_DB_PASSWORD=' .env | cut -d= -f2-)@postgres:5432/lexloom"

docker compose exec -e DATABASE_URL="$MIG" api python -m alembic upgrade head
docker compose exec -e DATABASE_URL="$MIG" api python -m alembic current   # must print: 0012 (head)
```

Without that DSN, `alembic current` fails with `permission denied for table
alembic_version` — the application role cannot read the version table, which is the
isolation design working rather than a misconfiguration.

Check `alembic current`, not the exit code. A container built before a migration existed
will report success and apply nothing.

### Then load the template corpus, before anyone can sign in

Every demo tenant is created holding the whole GDPR, copied out of a template
organisation. This is what fills that organisation, and until it has run, **every
sign-in fails** with `TemplateCorpusMissingError` — deliberately, because the
alternative is a visitor whose tenant quietly holds nothing.

```bash
docker compose exec -T api python scripts/seed_corpus.py
# Seeded 30 excerpt chunks into organisation 00000000-0000-0000-0000-000000000001.
# Seeded 978 chunks of gdpr-full.
```

Two things about it. It is **idempotent** — each loader replaces its own rows, so
re-running on every deployment leaves one copy, and the two loaders no longer care
which order they run in. And it takes **minutes, not seconds**: it parses the PDF and
runs 978 embeddings, on purpose, so that no visitor's sign-in ever pays for it.

Re-run it whenever the corpus changes — a new catalogue document, a re-parsed source, a
different embedding model. Nothing detects a stale template: the rows are a copy taken
whenever this last ran, and tenants keep receiving the old one.

**Capacity, changed by this.** `INGEST_ORG_CAP` (8) no longer bounds the GDPR — it only
bounds the second document. Every live tenant now holds ~978 chunks for its full
`DEMO_ORG_TTL` of 24 h, at roughly 2.4 KB a chunk, so ~2.4 MB per visitor per day
(derived from a 384-dimensional float32 vector plus ~500 characters of text, not
measured end to end). A hundred visitors in a day is ~240 MB of Postgres growth resident
at the peak, on a box shared with other applications. The lever for that is the TTL, not
the cap.

## Step 4 — build the frontend

```bash
cd /var/www/portfolio/lexloom/app/frontend
npm ci
npm run build            # emits ./out — a directory of files, no server
mkdir -p /var/www/portfolio/lexloom/web
cp -r out/. /var/www/portfolio/lexloom/web/
```

No `sudo` here, unlike the steps either side of it. `/var/www/portfolio` belongs to the
deploy user, so the export is written by the same account that built it. nginx still reads
it: every ancestor grants traverse to all (`/var/www` 755 root, `/var/www/portfolio` 755
deploy), and the export itself is world-readable.

## Step 5 — nginx (requires sudo)

```bash
sudo mkdir -p /etc/nginx/snippets
sudo cp /var/www/portfolio/lexloom/app/deploy/nginx/snippets/lexloom-proxy.conf   /etc/nginx/snippets/
sudo cp /var/www/portfolio/lexloom/app/deploy/nginx/snippets/lexloom-headers.conf /etc/nginx/snippets/

# Permissions BEFORE the secret is written, not after. `cp` lands the file 0644,
# and this file is about to hold a secret while every other file in /etc/nginx is
# 0644. Left at the default it would be readable by every local account on the box
# — including whatever runs the neighbouring applications, which is precisely the
# party the secret exists to tell apart from nginx. nginx parses its config as root
# before dropping to www-data, so it never needs the read bit for others.
# `sed -i` below preserves the mode and owner of the file it rewrites, so doing
# this first leaves no window in which the secret sits in a world-readable file.
sudo chown root:root /etc/nginx/snippets/lexloom-proxy.conf
sudo chmod 640       /etc/nginx/snippets/lexloom-proxy.conf

# The shared secret, into the INSTALLED copy — the same string as
# TRUSTED_PROXY_SECRET in .env. The committed file carries a placeholder that
# the application refuses to start with, so forgetting this fails loudly.
#
# The sed script arrives on **stdin**, and that is the point of the pipe. Written
# the obvious way — `sudo sed -i "s|...|$SECRET|" file` — the expanded secret is
# an element of sed's argv, and argv is readable through /proc/<pid>/cmdline by
# every local account on this box: exactly the accounts the paragraph above is
# about. `printf` is a shell builtin, so it forks nothing and puts nothing on any
# command line either. A hex secret (`openssl rand -hex 32`) contains no `|`, `&`
# or backslash, so it needs no escaping as a sed replacement.
SECRET=$(grep '^TRUSTED_PROXY_SECRET=' /var/www/portfolio/lexloom/app/.env | cut -d= -f2-)
test -n "$SECRET" || { echo 'TRUSTED_PROXY_SECRET is not set in .env'; exit 1; }
printf 's|REPLACE_ME_WITH_TRUSTED_PROXY_SECRET|%s|\n' "$SECRET" |
  sudo sed -i -f - /etc/nginx/snippets/lexloom-proxy.conf
unset SECRET
sudo grep -c REPLACE_ME /etc/nginx/snippets/lexloom-proxy.conf   # must print 0

sudo cp /var/www/portfolio/lexloom/app/deploy/nginx/lexloom.conf /etc/nginx/sites-available/lexloom
sudo ln -s /etc/nginx/sites-available/lexloom /etc/nginx/sites-enabled/lexloom
sudo nginx -t && sudo systemctl reload nginx
```

**Copying `lexloom.conf` again, later, destroys the TLS configuration.** Step 6 has
certbot rewrite the installed file in place: it strips `listen 80` from the block it
converts, adds `listen 443 ssl` with the certificate paths, and appends a second block
that redirects HTTP. The committed file knows nothing about any of that, so overwriting
the installed copy silently reverts the site to plain HTTP — `nginx -t` passes, and the
first symptom is a browser refusing to connect over HTTPS.

Restore it without contacting Let's Encrypt, and do not reload in between:

```bash
sudo cp $APP/deploy/nginx/lexloom.conf /etc/nginx/sites-available/lexloom
sudo certbot install --nginx --cert-name lexloom.sigmalion.io   # re-adds the :443 block
sudo nginx -t && sudo systemctl reload nginx
```

`certbot install` reuses the certificate already on disk. `certbot --nginx` would work
too, but it is the issuance path, and issuance has rate limits that a config edit has no
reason to spend.

**Redo the `sed` after every redeploy that copies the snippet again.** A mismatch between
the two copies does not break the site and does not raise: it makes the application stop
believing `X-Forwarded-For`, so every visitor on earth is metered on nginx's own address
and shares one budget of 30 logins an hour. The check below is how you notice.

## Step 6 — TLS (requires sudo)

```bash
sudo certbot --nginx -d lexloom.sigmalion.io
```

certbot rewrites the vhost in place and adds the `:443` block. It leaves the proxy
settings alone; re-read the file afterwards anyway and confirm `proxy_buffering off` is
still present in the snippet include.

---

## Verify — and verify the streaming specifically

Start by counting containers, not by calling the API. The API answering proves that the
API is up and nothing else: each service reads its own environment, so one of them can
fail to construct its settings and exit while the other four look perfectly healthy.
That is not a hypothetical — a demo once ran with no worker at all, and every check in
this section except this one passed.

```bash
docker compose ps
```

**All five of `postgres`, `redis`, `api`, `worker` and `beat` must read `Up`** (postgres
and redis `Up (healthy)`). Every service is `restart: unless-stopped`, so a container
that cannot start reads `Restarting` rather than `Exited` — count that as down, and read
its reason:

```bash
docker compose ps --status running --format '{{.Service}}' | sort   # expect all five
docker compose logs worker | tail -20                               # if one is missing
```

A settings error names the setting in the last line of the log.

```bash
curl -fsS https://lexloom.sigmalion.io/health/ready
```

`ready` means the process can reach Postgres and Redis. **It does not mean the schema
exists** — a fresh database with no migrations also answers `ready`. Trust
`alembic current` for that. It says nothing at all about the worker: nothing the API
serves touches it, which is why the container count comes first.

Now the part that actually matters. Streaming is what this project is about, and the
failure mode is that it works locally and buffers in production:

```bash
# 1. get a tenant
TOK=$(curl -fsS -X POST https://lexloom.sigmalion.io/api/v1/auth/demo-login \
      -H 'content-type: application/json' -d '{"label":"Deploy check"}' | jq -r .access_token)
# 2. a conversation and a question
CONV=$(curl -fsS -X POST https://lexloom.sigmalion.io/api/v1/conversations \
      -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
      -d '{"title":"check"}' | jq -r .id)
MSG=$(curl -fsS -X POST https://lexloom.sigmalion.io/api/v1/conversations/$CONV/messages \
      -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
      -d '{"content":"What are the processor obligations?"}' | jq -r .message_id)
TICKET=$(curl -fsS -X POST https://lexloom.sigmalion.io/api/v1/auth/stream-tickets \
      -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
      -d "{\"message_id\":\"$MSG\"}" | jq -r .ticket)
# 3. watch it arrive
curl -N -s "https://lexloom.sigmalion.io/api/v1/conversations/$CONV/messages/$MSG/stream?ticket=$TICKET" \
  | while IFS= read -r line; do printf '%s  %s\n' "$(date +%S.%N | cut -c1-6)" "$line"; done
```

Read the timestamps, not the output. **Tokens must arrive spread over about a second.**
If every line shares a timestamp, the answer was buffered and released in one flush —
which is precisely the bug this project exists to fix, and it means `proxy_buffering off`
is not in effect on the path that served the request.

Now ingest a real corpus, reusing `$TOK` from above. `POST /api/v1/ingest` only admits
and enqueues — the parsing, chunking and embedding run in the worker, staged through
`chunks_done`/`chunks_total`, so polling `GET .../ingest/status` is how you watch it
finish rather than how you find out it started:

```bash
curl -fsS -X POST https://lexloom.sigmalion.io/api/v1/ingest \
  -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
  -d '{"source_key":"gdpr-full"}' | jq
# {"stage":"queued", "chunks_done":0, "chunks_total":0, "error":null, "started_at":...}
#
# The POST answers with THIS source's status. The GET answers with a map keyed by
# source_key — one entry per catalogue document, `idle` for one never touched:
# curl -fsS .../api/v1/catalogue | jq -r '.[].key'   # gdpr-full, nist-sp-800-63b

while :; do
  S=$(curl -fsS https://lexloom.sigmalion.io/api/v1/ingest/status \
      -H "authorization: Bearer $TOK" | jq -r '."gdpr-full".stage')
  echo "$S"; [ "$S" = complete ] || [ "$S" = error ] && break
  sleep 1
done
```

Expect `stage` to move `queued` → `parsing` → `chunking` → `embedding` → `indexing` →
`complete`, `chunks_total` to hold once parsing finishes (`978` for `gdpr-full` and `746`
for `nist-sp-800-63b` as this file is written — read them back from the response rather
than trusting these numbers, since the parser that produces them is exactly the kind of
thing a later change moves; this sentence said `1027` until the extractor was replaced,
which is the caveat proving itself), and `chunks_done` to climb to meet it.

A second `POST` for the **same document** answers `409 already_ingested`. A second POST for
the *other* document is admitted: a tenant may hold every source in the catalogue, one
ingest per (tenant, source). `GET .../ingest/status` returns one entry per catalogue key,
so `jq -r '."gdpr-full".stage'` rather than `jq -r .stage`.

The whole job runs against `task_soft_time_limit=300` / `task_time_limit=360`
(`backend/app/worker/celery_app.py`); the GDPR round trip is measured at ~75 s wall-clock
outside container CPU limits (`backend/tests/test_ingest_job.py`) and the NIST one is
smaller in proportion to its 746 chunks, so give either a couple of minutes on this box
before treating a `queued`/`parsing` stage as stuck. An organisation with less life left
than the task is allowed to take is refused outright rather than started and killed by the
purge — see migration 0011.

Then confirm the ceilings are on and that the proxy secret matches. The application says
both, once, at start-up:

```bash
docker compose logs api | grep -E '"event": "limits_(enabled|disabled)"'
```

Expect **`limits_enabled`** with `"env": "demo"` and `"proxy_secret_required": true`.
`limits_disabled` means the box is unmetered. A missing `X-LexLoom-Proxy` — the mismatch
described in step 5 — shows up per request instead:

```bash
docker compose logs api | grep forwarded_for_without_proxy_secret   # expect nothing
```

Then watch memory, because this box is shared:

```bash
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'
free -h
```

Budget: postgres 384 MB, redis 64 MB, api 450 MB, worker 650 MB, beat 96 MB — about
**1.64 GB** ceiling against 2.2 GB free. The margin is thin on purpose; if something sits
near its limit, lower it rather than raising the total.

These are RAM figures and they stay RAM figures after Step 2. `mem_limit` maps to
Docker's `--memory`, which caps a container's resident memory; the swapfile lets a
container that reaches its limit spill into swap instead of being killed there and then,
but it does not raise the 1.64 GB. Read `docker stats` for the per-container number and
`free -h` for the box — and treat a *growing* `Swap: used` as the signal it is, because
by then the whole host is paying for it.

---

## Metrics — `GET /api/v1/status`

No Prometheus and no `/metrics` on this box, and that is deliberate: the target is
another operator's shared host, and a Prometheus exposition endpoint publishes queue
depth, connection-pool internals and every other figure this deployment has decided not
to hand a stranger. Instead there is one public, unauthenticated endpoint:

```bash
curl -fsS https://lexloom.sigmalion.io/api/v1/status | jq
```

It answers with the commit running (`GIT_SHA`, set above), process uptime, request
counts by status class, and two separate percentile distributions — one for ordinary
requests, one for how long SSE streams stayed open, because a stream is timed by the
model and counting it as latency would report "the slowest route" as the fastest one.
Both distributions are process-local: they live in the memory of the one uvicorn worker
this Dockerfile runs, reset on every restart, and are read straight off that number
rather than out of Redis — see the comment beside `Registry` in
`backend/app/observability.py` for why.

What it deliberately does **not** report: how many of the sixteen stream slots are in
use, anything broken out per organisation, queue depth, or any host-level figure such as
load average or free memory — the same box-is-shared reasoning as everywhere else in
this file. `docs/api-contract.md` §9 has the full list and the reasoning behind each
omission.

## Redeploying

```bash
cd /var/www/portfolio/lexloom/app && git pull

# Build, do NOT restart yet. A new migration is invisible to a container built
# before it existed: `alembic upgrade head` would report success and do nothing.
GIT_SHA=$(git rev-parse --short HEAD) docker compose build

# Drain first. The ingest progress key gained a `source_key` segment, so a job
# in flight across the change is orphaned: its hash lives under the old key,
# the new API reads the new one, sees nothing, and the panel offers the button
# again. Nothing corrupts — the per-source write guard still refuses the
# duplicate — but the tenant pays for a full parse and embed to be told no.
docker compose exec redis redis-cli --scan --pattern 'lexloom:ingest:*'

export MIG="postgresql+asyncpg://lexloom_migrator:$(grep '^MIGRATOR_DB_PASSWORD=' .env | cut -d= -f2-)@postgres:5432/lexloom"
docker compose run --rm -e DATABASE_URL="$MIG" api python -m alembic upgrade head
docker compose run --rm -e DATABASE_URL="$MIG" api python -m alembic current   # expect 0011 (head)

# Schema first, then producers, then consumers. Migrations 0009-0011 are
# additive and backward-compatible — the old code inserts pages and ignores
# `locator` — so the running containers keep working while the schema is ahead
# of them. The reverse (new code, old schema) is what breaks, which is why the
# migration runs before anything restarts.
docker compose up -d --no-deps worker beat
docker compose up -d --no-deps api

cd frontend && npm ci && npm run build && cp -r out/. /var/www/portfolio/lexloom/web/

# Confirm the worker against its budget. Measured on a workstation, one
# process, real weights: the .docx path peaks at 473.3 MiB (746 chunks, 30.0 s)
# and the PDF path at 482.1 MiB (978 chunks, 41.8 s), from a 79.3 MiB baseline
# before either parse. Against the 650m limit that is ~168 MiB of headroom, and
# the worker runs --concurrency=1 so the two never overlap. Container CPU
# limits make this box slower than that measurement, not hungrier.
docker stats --no-stream; free -h
```

`docker compose run --rm` rather than `exec`, so the migration does not depend on a
container already being up — and note this still puts the DSN on the process argv, where
`/proc/<pid>/cmdline` is world-readable. That is a known defect carried from the previous
milestone, not something this one introduced.

## What is deliberately not installed here

**LibreOffice.** `backend/scripts/build_docx_source.sh` uses it to build the committed
`.docx` from NIST's HTML, and that is a build step run by a human on a workstation. Nothing
at runtime and nothing in CI invokes it. Do not install it on this box: it is a large
package on a machine with 2.2 GB of RAM and no swap, and the artefact it produces is already
in the repository with both of its digests pinned.

## Rolling back

```bash
cd /var/www/portfolio/lexloom/app
git checkout <previous-sha>
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

Migrations are **not** rolled back automatically. Every migration in this repository has
a working `downgrade()`, but run one only if the previous revision genuinely cannot read
the current schema — downgrading a migration that dropped data does not bring it back.

## Turning it off

```bash
cd /var/www/portfolio/lexloom/app && docker compose down          # keeps the data volume
sudo rm /etc/nginx/sites-enabled/lexloom && sudo systemctl reload nginx
```

**This box is not ours alone, and Docker on it is not ours alone.** A production
`marzban` container has been running here since long before this project existed, on the
same daemon. Every Docker command in this file is run from
`/var/www/portfolio/lexloom/app` for that reason — Compose then scopes itself to this
project and cannot touch anything else.

Three commands break that scoping and must not be run on this host:

| Command | What it actually does here |
|---|---|
| `docker compose down` outside the project directory | acts on whatever Compose project the current directory resolves to, or none |
| `docker system prune -a` | deletes the neighbour's image; it is not "unused" merely because our containers do not use it |
| `docker volume prune` | deletes any volume no *running* container holds — including ours while the stack is stopped |

Removing this project's data is `docker compose down -v`, from this project's directory,
and nothing else.

---

## Known gaps, stated rather than discovered

- **Demo tenants are not free.** Each one copies a 30-chunk corpus: about 31 rows and
  11.7 kB per anonymous `demo-login`. Watch the row count for the first days after this
  goes public.
- **`/health/ready` does not check the schema.** See above.
- **The `X-Forwarded-For` handling assumes exactly one proxy** — this nginx. The snippet
  overwrites the header rather than appending to it, so the application receives one
  address it can trust. Adding another proxy in front means revisiting both the snippet
  and the code that reads it.
- **The proxy secret lives in two files and nothing checks that they agree.** `.env` and
  the installed nginx snippet. A mismatch is silent in the sense that nothing fails: it
  degrades the rate limit to one shared budget for every visitor. The start-up line and
  the `forwarded_for_without_proxy_secret` warning above are how it is caught.
- **`docker compose up -d --build` will not start the API with `ENV=demo` unless
  `AUTH_JWKS_URL` is HTTPS and `TRUSTED_PROXY_SECRET` is set.** That is the intent — a
  public box that cannot meet those does not belong up — but it means a half-edited
  `.env` shows as a container that never reaches `Up` rather than as a warning.
  `docker compose logs api` names the setting. Read `docker compose ps` first, as above:
  the check is per service, so a stack can be three-quarters up and answer every request
  you think to make.
