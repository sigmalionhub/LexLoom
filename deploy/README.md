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
| Redis | container | `127.0.0.1:6379` only |
| API (FastAPI) | container | `127.0.0.1:8100` only |
| Worker (Celery) | container | no ports |
| Frontend | **files on disk** | statically exported, served by nginx — no Node process |
| nginx | host, already installed | terminates TLS, serves the files, proxies the API |

The frontend and the API share one origin. That is a requirement, not a preference: the
backend ships **no CORS headers at all**, so a frontend on a second origin could not call
it.

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

The passwords are read **only when the data volume is first created**. Changing them later
edits nothing inside a Postgres that already exists, so set them now.

```bash
docker compose up -d --build
```

Then apply migrations. **The API container cannot do this**, by design — it connects as an
unprivileged role that owns nothing, which is what makes the tenant isolation claim
enforceable. Migrations run as the migration role, so both commands below carry its DSN:

```bash
export MIG="postgresql+asyncpg://lexloom_migrator:$(grep '^MIGRATOR_DB_PASSWORD=' .env | cut -d= -f2-)@postgres:5432/lexloom"

docker compose exec -e DATABASE_URL="$MIG" api python -m alembic upgrade head
docker compose exec -e DATABASE_URL="$MIG" api python -m alembic current   # must print: 0004 (head)
```

Without that DSN, `alembic current` fails with `permission denied for table
alembic_version` — the application role cannot read the version table, which is the
isolation design working rather than a misconfiguration.

Check `alembic current`, not the exit code. A container built before a migration existed
will report success and apply nothing.

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

# The shared secret, into the INSTALLED copy — the same string as
# TRUSTED_PROXY_SECRET in .env. The committed file carries a placeholder that
# the application refuses to start with, so forgetting this fails loudly.
SECRET=$(grep '^TRUSTED_PROXY_SECRET=' /var/www/portfolio/lexloom/app/.env | cut -d= -f2-)
test -n "$SECRET" || { echo 'TRUSTED_PROXY_SECRET is not set in .env'; exit 1; }
sudo sed -i "s|REPLACE_ME_WITH_TRUSTED_PROXY_SECRET|$SECRET|" \
  /etc/nginx/snippets/lexloom-proxy.conf
grep -c REPLACE_ME /etc/nginx/snippets/lexloom-proxy.conf   # must print 0

# This file now holds a secret, and every other file in /etc/nginx is 0644.
# Left at the default it would be readable by every local account on the box —
# including whatever runs the neighbouring applications, which is precisely the
# party the secret exists to tell apart from nginx. nginx parses its config as
# root before dropping to www-data, so it never needs the read bit for others.
sudo chown root:root /etc/nginx/snippets/lexloom-proxy.conf
sudo chmod 640       /etc/nginx/snippets/lexloom-proxy.conf

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

## Redeploying

```bash
cd /var/www/portfolio/lexloom/app && git pull
docker compose up -d --build
export MIG="postgresql+asyncpg://lexloom_migrator:$(grep '^MIGRATOR_DB_PASSWORD=' .env | cut -d= -f2-)@postgres:5432/lexloom"
docker compose exec -e DATABASE_URL="$MIG" api python -m alembic upgrade head
docker compose exec -e DATABASE_URL="$MIG" api python -m alembic current   # check BEFORE assuming it ran
cd frontend && npm ci && npm run build && cp -r out/. /var/www/portfolio/lexloom/web/
```

A new migration is invisible to a container built before it existed. `alembic upgrade
head` will then report success and do nothing. Always confirm with `alembic current`.

## Rolling back

```bash
cd /var/www/portfolio/lexloom/app
git checkout <previous-sha>
docker compose up -d --build
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
