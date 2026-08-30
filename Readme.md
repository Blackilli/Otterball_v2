# Otterball v2 🦦🏆
Thank you clanker for the Readme <3

Otterball v2 is a dockerized sports prediction platform — built around the FIFA World Cup, and now also running NFL pools — pairing a Discord bot with a Django web side that publishes the same data as read-only public pages.

Predictions are made in Discord and nowhere else: the bot posts a native poll per fixture, closes it at kickoff, scores it against ingested match data, and keeps a pinned leaderboard ranked by the official Standard Competition Ranking (1-2-2-4). Match ingestion and scoring run asynchronously on a Celery cluster.

## ⚡ Core Features

- **Automated Match Day Polls:** Posts one native Discord poll per upcoming fixture directly into the pool channel and pins it, on a per-pool schedule (weekday, time and lookahead window). Each poll's duration is capped to close exactly at kickoff — which also caps a batch at Discord's 32-day maximum poll duration.
- **Live Match Ticker:** One message per poll, edited in place through its whole lifecycle (Components V2): a pre-kickoff reminder naming role members who still have no pick, then the live score, then the final result plus everyone who called it. Editing rather than reposting means only the first post pings anyone, and the message's own **Notification settings** button opens a form where each player switches their reminders for that pool on or off.
- **True Sports Leaderboard (Standard Competition Ranking):** Computes rankings mathematically. If two players share 1st place, 2nd place is skipped, and the next player lands directly on 3rd place. The formatting prevents breaking Discord's character limits via dynamic *Pleb-Splitting*.
- **Real-Time Reconciliation:** Votes are captured live from Discord's poll events, then fully re-derived from the poll itself twice — once on bot startup for every open poll, and once at kickoff after the poll is closed. That second pass is final (a closed poll cannot change) and deletes votes retracted while the bot was offline, which is what makes the pipeline tamper-proof.
- **Garbage Removal System:** Removes Discord's own noise from the pool channel — *"The poll results are in!"* and the bot's *"X pinned a message"* notices — both live and via a historical boot sweep, and only in channels the bot was actually pointed at.
- **Backup & Restore:** `export_db` / `import_db` management commands bundle the full database *and* the media files into one compressed archive and restore it cleanly, skipping derived/ephemeral tables (contenttypes, sessions, admin logs, Celery task results). The database half uses natural keys, so it restores into an empty database on another server without renumbering anything that matters.
- **Multi-Sport Ingestion:** Soccer comes from the FIFA API; the NFL is deliberately dual-sourced from ESPN (schedule plus live scores) and nflverse (an independent backstop for final results, cross-referenced by ESPN event id). Every provider id lives in its own mapping table, so ingestion is idempotent and a new provider needs no changes to the core models.
- **Public Web Pages:** Read-only and season-scoped — upcoming fixtures, the knockout bracket, pool standings, and a stats page whose rank-over-time chart is server-rendered SVG (complete with JavaScript off). The standings read the very same ranking code as the bot's pinned message, so the site and Discord cannot disagree about who is second.
- **Season Opener:** One Components V2 card per pool, posted by the bot when a pool is bound to a channel: the three steps of playing, what each round is worth, links to the season on the web, and the notification opt-out as a button on the card itself. It is edited in place as the pool's settings change, so correcting the points never pings the role twice.
- **Guided Pool Setup:** `manage.py create_pool` / `check_pool`, or the admin's *Start a new pool* page, which ends in the same readiness report — because almost every way a pool can be misconfigured is otherwise silent.
- **Message Previews:** *Preview messages* on a pool's admin page posts a chosen fixture's poll, reminder, live score and full-time result into its channel so you can see a match night before one happens. They are built by the code that runs the real thing, ping nobody, score nothing, and are deleted again from the same page. A *Buttons & modals* message posts every clickable component as the live one, which is also the only way to open a modal — Discord opens those from an interaction and never on their own.
- **Modern Deployment:** Fast Docker builds leveraging the modern `uv` package manager and BuildKit caching.

## 🛠️ Tech Stack

- **Backend Runtime:** Python 3.14-slim
- **Web Framework:** Django 6.0+ (including WhiteNoise for optimized static asset delivery in production mode)
- **ASGI/WSGI Server:** Gunicorn
- **Asynchronous Tasks & Scheduler:** Celery & Celery Beat
- **Database:** PostgreSQL 18-alpine (optimized for the new cluster layout to allow seamless major upgrades)
- **In-Memory Cache & Broker:** Valkey 8-alpine (a fully compatible, modern open-source Redis successor)
- **Bot Library:** `discord.py` (fully asynchronous operation via Django's Async-ORM)
- **Package Management:** `uv` by Astral

---

## 🚀 Production Deployment (Quick Start)

Since the package is public, your production server can pull the pre-built image directly from the GitHub Container Registry (GHCR) without needing any authentication.

### 1. Prerequisites
Ensure Docker Engine and the Docker Compose plugin are installed on your host system.
Follow [Docker's own install guide](https://docs.docker.com/engine/install/) — it sets up
the apt repository and installs both. With that repository already configured:
```bash
sudo apt-get update && sudo apt-get install -y docker-ce docker-compose-plugin
```

Valkey also asks the host for memory overcommit; without it a background save can
fail under memory pressure, and it says so in the log on every boot:

```bash
echo 'vm.overcommit_memory = 1' | sudo tee /etc/sysctl.d/99-valkey.conf
sudo sysctl vm.overcommit_memory=1
```

### 2. Setup Environment Variables (`.env`)
Create a `.env` file in your deployment directory.

*⚠️ IMPORTANT: If your `DJANGO_SECRET_KEY` or your Discord token contains special characters like `#` (comment parser) or `$` (variable parser), you must wrap the key in single quotes (`'...'`)!*

```env
# Django Settings
ENV=production
DJANGO_SECRET_KEY='your-secret-key-containing-#-or-$'
# Must keep 127.0.0.1: the web container's healthcheck probes /health/ over it,
# and Django answers 400 for a host it was not told to allow.
ALLOWED_HOSTS=your-domain.com,127.0.0.1
TZ=Europe/Berlin

# Where the public pages are reachable from outside. The bot links each pool's
# season - fixtures, leaderboard, stats - from its welcome post, and a Discord
# message has no request to build an absolute URL from.
PUBLIC_SITE_URL=https://your-domain.com

# Host/container port the web service listens on and is published under
# (gunicorn binds it, the healthcheck probes it, compose publishes it).
WEB_PORT=8000

# Host address that port is published on. 127.0.0.1 is loopback only, i.e.
# reachable through the Nginx in step 4 and nothing else; 0.0.0.0 is every IPv4
# address on the machine, `::` every IPv6 one. Serving the app straight off the
# internet this way means no TLS and no proxy in front of it - and ALLOWED_HOSTS
# has to name whatever people type, an IPv6 literal in brackets (`[2001:db8::1]`)
# included.
WEB_BIND=127.0.0.1

# Celery prefork children. These tasks wait on HTTP rather than compute, and
# every child is a full Django process (~60 MB), so the default of one per core
# buys nothing on a small VM.
CELERY_CONCURRENCY=2

# User the containers run as. Set these to the owner of your `./media` folder
# (`id -u` / `id -g`) so ingested team logos are not written as a foreign user.
PUID=1000
PGID=1000

# PostgreSQL 18 Configuration (consumed by the `db` container)
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_database_password
POSTGRES_DB=otterball_db
# Only the port Postgres is published under on the host - the containers always
# talk to it on 5432 inside the network. Change it if 5432 is already taken.
POSTGRES_PORT=5432

# This is what Django/Celery actually connect with — it must match the
# POSTGRES_* values above and point at the `db` service by its container name.
DATABASE_URL=postgresql://postgres:your_secure_database_password@db:5432/otterball_db

# Discord API
DISCORD_BOT_TOKEN=your_discord_bot_token

# Cache & Celery broker (Valkey/Redis). CELERY_BROKER_URL defaults to REDIS_URL
# and CELERY_RESULT_BACKEND defaults to Django's DB backend, so both are optional.
REDIS_URL=redis://valkey:6379/0
```

*Note: `ENV=production` is what disables `DEBUG` (see `otterball_v2/settings.py`) — there is no separate `DEBUG` variable.*
*Note: if `DATABASE_URL` is left unset, Django silently falls back to a local SQLite file — always set it explicitly in production.*

#### Generating the secrets

Two of these values must be generated, one is handed to you by Discord, and none
of them should ever be committed:

```bash
# DJANGO_SECRET_KEY - hex output, so it can never contain a `#` or `$` and needs
# no quoting in the .env file:
openssl rand -hex 64

# ...or, if you already have the project checked out, Django's own generator
# (its alphabet does include `#` and `$`, so wrap the result in single quotes):
uv run python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# POSTGRES_PASSWORD - hex again, because this value also has to survive being
# pasted into the DATABASE_URL below, where `@`, `:` and `/` would break parsing:
openssl rand -hex 32

# PUID / PGID - not secret, just the user owning your ./media directory:
id -u; id -g
```

Then mirror the password into `DATABASE_URL` — it is a second copy of the same
secret, and the two silently drifting apart is the most common setup failure:

```env
POSTGRES_PASSWORD=6f1c...   # from `openssl rand -hex 32`
DATABASE_URL=postgresql://postgres:6f1c...@db:5432/otterball_db
```

`DISCORD_BOT_TOKEN` is **not** generated locally: create an application at
<https://discord.com/developers/applications>, add a bot, and copy its token
(*Bot → Reset Token* — Discord only shows it once). While you are on that page,
enable **Server Members Intent**, which the bot requires to log in at all.

Keep `.env` out of version control (`.gitignore` already covers it) and readable
only by you: `chmod 600 .env`. If a token or key does leak, rotate it — Discord
regenerates a token on *Reset Token*, and rotating `DJANGO_SECRET_KEY` only
invalidates existing sessions.

### 3. Create the Directories & `compose.yml`
Everything that has to survive a `compose pull` is a bind mount, so all of it is
visible on the host — backed up with `tar`/`rsync`, and readable by an Nginx that
never enters a container:

```bash
mkdir -p media static pgdata backups
```

| Directory | Holds | Why a host folder |
|---|---|---|
| `media/` | Team crests, written by the worker | Nginx serves it directly (step 4), and it is the half `export_db` cannot reconstruct |
| `static/` | `collectstatic` output | Nginx can serve it without whitenoise, and you can inspect what was collected |
| `pgdata/` | The Postgres cluster | A file-level backup is `tar czf` on a stopped `db`, no `pg_dump` needed |
| `backups/` | `export_db` bundles | The bundle is on the host the moment it is written, ready to copy off the machine |

The containers start as root purely so their entrypoint can remap their internal
user to `PUID`/`PGID`, then drop to it via `gosu` — so with `PUID`/`PGID` set to
your own ids, everything the worker downloads into `./media` and everything
`collectstatic` writes into `./static` stays owned by you. Leave them unset and
the entrypoint chowns both directories to its built-in `8888:8888` instead, which
is exactly the root-owned-backup annoyance the bind mounts are meant to avoid.
`pgdata/` is the exception either way: the Postgres image runs as its own
`postgres` user and chowns that directory itself, so leave it out of `PUID`.

Create a `compose.yml` file next to your `.env` pointing to the official GHCR image:

```yaml
# Log rotation, so a long-running deploy cannot fill the disk with json-file logs.
x-logging: &default-logging
  logging:
    driver: json-file
    options:
      max-size: "10m"
      max-file: "3"

services:
  web:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    <<: *default-logging
    env_file: .env
    # No `command:` — the image's default one migrates, installs the Celery Beat
    # schedule, collects static files and then runs gunicorn on WEB_PORT.
    ports:
      # WEB_BIND is the host address: 127.0.0.1 keeps gunicorn behind the Nginx
      # in step 4, 0.0.0.0 serves every IPv4 address directly, `::` every IPv6
      # one. Docker binds `::` v6-only, so uncomment the second entry to serve
      # both stacks at once.
      - "${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8000}:${WEB_PORT:-8000}"
      # - host_ip: "::"
      #   target: ${WEB_PORT:-8000}
      #   published: "${WEB_PORT:-8000}"
      #   protocol: tcp
    volumes:
      - ./media:/app/media
      - ./static:/app/static
      # export_db writes here (BASE_DIR/backups); mounted so a bundle survives
      # the container it was created in.
      - ./backups:/app/backups
    # A cap turns a runaway container into one restart rather than an OOM that
    # takes the VM with it. Measured usage is well under each of these.
    mem_limit: 512m
    depends_on:
      db:
        condition: service_healthy
      valkey:
        condition: service_healthy
    healthcheck:
      # 127.0.0.1 rather than localhost: /health/ goes through ALLOWED_HOSTS,
      # which the .env above allows by ip.
      test: ["CMD-SHELL", "curl -f http://127.0.0.1:$${WEB_PORT:-8000}/health/"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 10s

  bot:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    <<: *default-logging
    env_file: .env
    command: uv run python manage.py runbot
    volumes:
      - ./media:/app/media
    mem_limit: 512m
    depends_on:
      db:
        condition: service_healthy
      valkey:
        condition: service_healthy
      # `web` is the container that migrates; see the note under this file.
      web:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "test $$(find /tmp/bot_heartbeat -mmin -2)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

  worker:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    <<: *default-logging
    env_file: .env
    command: uv run celery -A otterball_v2 worker --loglevel=info --concurrency=${CELERY_CONCURRENCY:-2}
    volumes:
      - ./media:/app/media
    mem_limit: 768m
    depends_on:
      db:
        condition: service_healthy
      valkey:
        condition: service_healthy
      web:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "uv run celery -A otterball_v2 inspect ping -d celery@$$HOSTNAME"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  beat:
    image: ghcr.io/blackilli/otterball_v2:latest
    restart: unless-stopped
    <<: *default-logging
    env_file: .env
    command: uv run celery -A otterball_v2 beat --loglevel=info
    mem_limit: 384m
    depends_on:
      db:
        condition: service_healthy
      valkey:
        condition: service_healthy
      web:
        condition: service_healthy
    healthcheck:
      # `[c]elery`, not 'celery beat': the process is `celery -A otterball_v2
      # beat`, and the brackets stop the check's own shell matching itself.
      test: ["CMD-SHELL", "ps aux | grep -q '[c]elery -A otterball_v2 beat'"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  db:
    image: postgres:18-alpine
    restart: unless-stopped
    <<: *default-logging
    env_file: .env
    # Postgres 18 keeps its cluster in /var/lib/postgresql/18/docker and declares
    # /var/lib/postgresql as its volume — mount that, NOT the /data subdirectory
    # older guides use, or every `compose up` after a `pull` starts empty.
    volumes:
      - ./pgdata:/var/lib/postgresql
    mem_limit: 1g
    # Uncomment to reach the database from the host; not needed by the cluster,
    # which always talks to it on 5432 over the compose network.
    # ports:
    #   - "127.0.0.1:${POSTGRES_PORT:-5432}:5432"
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  valkey:
    image: valkey/valkey:8-alpine
    restart: unless-stopped
    <<: *default-logging
    # No maxmemory: this is the Celery broker as well as the cache, and an
    # eviction here would silently drop queued tasks. The cap is the backstop.
    mem_limit: 256m
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
```

*Note: `depends_on` uses `condition: service_healthy` throughout. `bot`, `worker`
and `beat` additionally wait for `web`, because `web` is the container that runs
`migrate` — on a fresh database, `beat` otherwise dies on django_celery_beat's
missing tables and the worker's ingestion catch-up finds no `PredictionPool`.
Both recover on the next restart; waiting means neither has to.*

*Note: the `mem_limit` values are backstops, not budgets. Measured on an idle
cluster: web 113 MiB, worker 182 MiB at `CELERY_CONCURRENCY=2`, beat 107 MiB,
Postgres 34 MiB, Valkey 11 MiB. Raise a limit if `docker inspect` reports
`OOMKilled=true` for that service — a container hitting its own cap is a restart,
where the same total spread across an uncapped host is the VM going down.*

*⚠️ Migrating an existing deployment: if your `db` mounted
`postgres_data:/var/lib/postgresql/data` — the path older guides use — that
directory never held the Postgres 18 cluster, so the live data is in the
anonymous volume the container created for `/var/lib/postgresql`. Take an
`export_db` bundle **before** switching to `./pgdata`, then `import_db --flush`
into the new one; pointing the mount at an empty `./pgdata` starts an empty
database and initdb will happily populate it.*

### 4. Configure Production Nginx
Both asset folders are host directories, so Nginx can serve them without going
through Django at all:

```nginx
location /media/ {
    alias /path/to/your/deployment/media/;
    expires 30d;
}

location /static/ {
    alias /path/to/your/deployment/static/;
    expires 30d;
}
```

Whitenoise inside the container serves `/static/` too, so this is a performance
choice rather than a requirement — but it is also what lets you point any other
tool at the collected assets.

Everything else proxies to `WEB_PORT`, so if you change that value, change it here too:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### 5. Pull and Start the Application
Execute the following commands to pull the latest image and boot up the cluster:
```bash
sudo docker compose pull
sudo docker compose up -d
```

### 6. Create Your First Pool
A running cluster has no pool yet — and almost every way a pool can be misconfigured is
silent, so finish with the readiness check. Run these against the `web` container
(`sudo docker compose exec --user appuser web uv run python manage.py …` — `exec`
bypasses the entrypoint that drops privileges, so name the user yourself):

```bash
# 1. Sport data: competition, season, rounds, teams (logos + colours), schedule.
manage.py sync_nfl_infra                 # or sync_fifa_infra --sync-competitions …

# 2. The pool itself, its poll schedule and its points per round.
manage.py create_pool --name "NFL 2026" --sport AMERICAN_FOOTBALL --year 2026 \
    --weekdays 2 --time 18:00 --lookahead 7 --reminder-lead 60 \
    --points "Regular Season=1,Wild Card=2,Divisional=3,Conference Championship=4,Super Bowl=5"

# 3. Bind it to a guild + channel. The bot must have connected once before this:
#    it is what creates the guild/channel/role rows, so until it has, there is
#    nothing to bind to and the command refuses with a message saying so.
manage.py create_pool --name "NFL 2026" --sport AMERICAN_FOOTBALL --year 2026 \
    --guild <guild_id> --channel <channel_id> --notification-role <role_id>

# 4. Confirm it is actually ready (exits non-zero on FAIL, so it works as a deploy gate).
manage.py check_pool
```

Within a minute of step 3 the bot posts the season's welcome message and its pinned
leaderboard into the channel — no restart needed. The same flow has a guided page at
*Prediction pools → Start a new pool* in `/admin/`, and `create_pool` is idempotent, which
is why steps 2 and 3 are the same command.

Celery Beat is database-driven, so a task in the code does nothing until a `PeriodicTask`
row exists for it. The `web` container installs those with `ensure_schedule` on every start,
and a starting worker runs any overdue infrastructure sync once — so a spell with Beat down
does not strand a pool without fixtures. `check_pool` reports a stale schedule too.

### 7. Updates & Continuous Deployment
Whenever the GitHub Actions pipeline finishes baking a new version, update your live container stack by running:
```bash
sudo docker compose pull && sudo docker compose up -d
```

---

## 🛠️ Local Development

The repository ships its own `compose.yml` (services: `db`, `valkey`, `web`, `bot`, `worker`, `beat`), already configured to `build: .` from the local `Dockerfile` with named volumes, an internal `otterball_network`, and healthchecks for every service.

1. Clone the repository: `git clone https://github.com/Blackilli/Otterball_v2.git`
2. Create a `.env` file next to `compose.yml` (see the production `.env` example above — `DATABASE_URL` should point at `db` and `REDIS_URL` at `valkey`, e.g. `redis://valkey:6379/0`). `WEB_PORT`, `POSTGRES_PORT`, `PUID` and `PGID` all have defaults (`8000`, `5432`, `8888`, `8888`), so set them only if a port is taken or you bind-mount `media/` from the host.
3. Build and start the full cluster:
```bash
docker compose up -d --build
```

### Without Docker

Package management is via [`uv`](https://docs.astral.sh/uv/) (Python 3.14+ required). You'll need a local PostgreSQL and Redis/Valkey instance, plus `DATABASE_URL` and `REDIS_URL` pointing at them. Nothing in the Python code loads a `.env` file — that is compose's job — so export the variables into your shell yourself:

```bash
set -a && source .env && set +a                      # bash/zsh: load .env into the shell

uv sync                                              # install dependencies
uv run python manage.py migrate                      # apply migrations
uv run python manage.py ensure_schedule              # install the Celery Beat schedule
uv run python manage.py runserver                    # Django admin/web app
uv run python manage.py runbot                       # Discord bot
uv run celery -A otterball_v2 worker --loglevel=info # Celery worker
uv run celery -A otterball_v2 beat --loglevel=info   # Celery Beat scheduler

uv run python manage.py test                         # run the full test suite
```

Pre-commit hooks (pyupgrade, django-upgrade, yamlfix, black, isort, `uv lock`/`uv sync`, the full Django test suite, and gitleaks) run automatically on commit — expect commits to be slow or blocked if any of these fail.

### Database Backup & Restore

```bash
uv run python manage.py export_db                           # bundle DB + media to backups/otterball_<timestamp>.tar.gz
uv run python manage.py export_db dump.json.gz --no-media   # database only, as a plain fixture
uv run python manage.py import_db backups/otterball_xxx.tar.gz --flush   # restore, wiping existing rows first
```

`export_db` is the portable option: it uses natural keys, so a bundle restores
into an empty database on another machine and across a Postgres major version.
Against the production stack it runs in the `web` container and lands in the
host's `./backups`, since that folder is bind-mounted:

```bash
sudo docker compose exec --user appuser web uv run python manage.py export_db
sudo docker compose exec --user appuser web uv run python manage.py import_db \
    backups/otterball_20260830_180000.tar.gz --flush
```

`--user appuser` matters here: `docker compose exec` bypasses the entrypoint that
drops privileges, so without it the command runs as root and the bundle lands on
the host owned by root — the one file you actually want to copy off the machine.

There is also the file-level route — stop `db` first, or the copy is a torn
cluster:

```bash
sudo docker compose stop db
sudo tar czf pgdata-$(date +%F).tar.gz pgdata/ media/
sudo docker compose start db
```

That one is byte-identical and fast, but it only restores into the same Postgres
major version. `static/` needs no backup at all — `collectstatic` rebuilds it on
every start.

---

## 📦 CI/CD Pipeline (GitHub Actions)

The project includes an optimized GitHub Actions pipeline (`.github/workflows/build-image.yml`) that automatically triggers on every push or pull request to the `master` branch.

- **Registry:** GitHub Container Registry (`ghcr.io/blackilli/otterball_v2` — GHCR lowercases the repository name)
- **Caching:** Utilizes the native GitHub Actions cache backend (`type=gha`), ensuring that unchanged layers and the `uv` cache are reused. Subsequent builds typically complete in under 10 seconds.
- **Tags:** Every image is tagged with its branch or PR ref and the short Git commit SHA; builds on the default branch additionally receive the `latest` tag.
- **Pull requests build but never publish:** the push step is gated on `github.event_name != 'pull_request'`, so a PR only proves the image still builds.
- **Manual Trigger:** Enabled via `workflow_dispatch`, allowing you to manually force a build via the GitHub Web UI or GitHub CLI (`gh workflow run`) at any time.

Two additional workflows integrate [Claude Code](https://claude.com/product/claude-code) into the PR flow:

- **`claude-code-review.yml`** — automatically reviews every opened/updated pull request.
- **`claude.yml`** — responds to `@claude` mentions in issue comments, PR review comments, and issues, letting Claude act on request inside GitHub.
