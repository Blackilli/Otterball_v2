# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Otterball v2 is a Django + Discord bot sports prediction platform (built around the FIFA World Cup). Users submit match predictions via native Discord polls; a Celery cluster ingests match data from the FIFA API, computes points, and a bot posts/reconciles polls and a live leaderboard in Discord. The Django web side is intentionally thin — it exists almost entirely to serve `/admin/` (data management) and `/media/` (team logos); there are no real user-facing views (`sports/views.py`, `predictions/views.py`, `users/views.py`, `discord_bot/views.py` are all empty stubs).

## Commands

Package management is via `uv` (Astral). Python 3.14+ required.

```bash
uv sync                                    # install deps (--frozen --no-dev in production, see Dockerfile)
uv run python manage.py runserver          # run the Django admin/web app
uv run python manage.py runbot             # run the Discord bot (discord.py, fully async against Django's async ORM)
uv run celery -A otterball_v2 worker --loglevel=info   # run the Celery worker
uv run celery -A otterball_v2 beat --loglevel=info     # run Celery Beat (scheduler)

uv run python manage.py test               # run the full test suite
uv run python manage.py test sports        # run tests for a single app
uv run python manage.py test sports.tests.SomeTestCase.test_something  # run a single test

uv run python manage.py makemigrations --check   # CI/pre-commit checks for missing migrations (otterball_v2/tests.py enforces this in-suite too)
uv run python manage.py migrate

uv run black .
uv run isort .
uv run manage.py test   # also runs automatically as a local pre-commit hook (django-test)

uv run python manage.py export_db [output.json.gz]    # clean full DB export (natural keys, skips contenttypes/permissions/sessions/celery results)
uv run python manage.py import_db backups/foo.json.gz --flush   # restore a dump; --flush wipes existing rows first to avoid PK conflicts
```

Local Docker stack: `docker compose up -d --build` (services: `web`, `bot`, `worker`, `beat`, `db` (Postgres 18), `valkey` (Redis-compatible cache/broker)). All four app services (`web`, `bot`, `worker`, `beat`) build from the same image/Dockerfile and just run different commands — keep that in mind when a change needs to reach all of them.

Pre-commit hooks (`.pre-commit-config.yaml`) run pyupgrade, django-upgrade, yamlfix, black, isort, `uv lock`/`uv sync`, the full Django test suite, and gitleaks — expect commits to be slow/blocked if these fail. Formatting: black/isort with `line_length = 118`.

## Architecture

Four Django apps, each with a distinct responsibility:

- **`sports`** — the sport data domain: `Competition` → `Season` → `Stage` → `Match`, plus `Team`. Every one of these has a parallel `*Mapping` model (`CompetitionMapping`, `SeasonMapping`, `StageMapping`, `TeamMapping`, `MatchMapping`) that links a local row to an external provider's ID (currently only `SportsProvider.FIFA`). This mapping-table pattern is how `sports/services/ingestion.py` upserts data idempotently from `sports/integrations/fifa.py` (the FIFA API client) without duplicating rows — always look up/create via the mapping table, not by guessing a match on name/date.
- **`predictions`** — `PredictionPool` (a competition instance users play in) → `PoolConfiguration` (when/how polls get created) and `PoolStageRule` (points per correct pick, keyed by stage, with a `stage=None` row as the pool-wide fallback, and `3` as the final hardcoded fallback). `Prediction` ties a `User` + `Match` + `PredictionPool` to a predicted `MatchOutcome`, and knows how to score itself (`update_points`/`aupdate_points`, sync and async variants — keep both in sync if you touch the scoring logic). `PredictionPool.aget_leaderboard()` implements Standard Competition Ranking (1-2-2-4): ties share a rank and the next rank skips accordingly.
- **`discord_bot`** — mirrors real Discord entities (`DiscordGuild`, `DiscordChannel`, `DiscordGuildRole`) plus app-specific state: `DiscordProfile` (1:1 with `users.User`), `ActiveMatchMessage` (the live link between a `Match`, a guild's poll thread/message, and its leaderboard/ticker message state), `DiscordGuildPool` (which pool is active in which guild/channel, with an optional notification role and pinned leaderboard message), and `DiscordTeamEmoji` (custom emoji per team, used on poll buttons/leaderboard).
- **`users`** — thin `AbstractUser` subclass with a `uuid` field and an `is_discord_linked` convenience property.

### Data flow

1. **Ingestion** (`sports/tasks.py`, scheduled via Celery Beat / `django_celery_beat`): `sync_daily_infrastructure` walks competitions → national teams → seasons → stages → upcoming matches; `sync_live_games` polls only matches that are `LIVE` or `SCHEDULED` within 15 minutes of kickoff. All ingestion functions are async and live in `sports/services/ingestion.py`; they build local dicts as caches (`*_mapping_cache`) before looping to avoid N+1 queries against Postgres — follow this pattern rather than querying inside a loop. `sports/management/commands/sync_fifa_infra.py` exposes each step as a manual `--sync-*` CLI flag for backfills.
2. **Poll creation** (`discord_bot/cogs/poll_creation.py`, `PollCreationCog`): a self-adjusting `tasks.loop` that re-reads all active pools' configured `poll_creation_time`s from the DB every minute (`interval_sync_loop`) and reconfigures the actual posting loop's fire times (`poll_creation_loop`) accordingly — new/changed `PoolConfiguration` rows take effect without a bot restart. Poll answer ordering (draw included or not) is decided by `Stage.stage_type` via `DISCORD_POLL_ANSWER_ORDER_MAP` in `discord_bot/constants.py`.
3. **Vote capture** happens two ways and both must stay consistent: live, via `on_raw_poll_vote_add`/`_remove` in `discord_bot/cogs/poll_listener.py` (writes/deletes a `Prediction` per vote event), and in bulk via `ReconciliationCog.reconcile_active_polls()` on bot startup, which re-derives every prediction from `message.poll.answers[].voters()` for any `ActiveMatchMessage` not yet finalized — this is the "tamper-proof" reconciliation pass mentioned in the README.
4. **Scoring**: `sports/signals.py` publishes a `MatchUpdatePayload` to Redis (`settings.REDIS_MATCH_UPDATE_TOPIC`) on every `Match` save; `predictions/signals.py`'s `receive_match_update` (fired via `transaction.on_commit`, only when a match transitions to `FINISHED`) recomputes points for every `Prediction` on that match using a pool/stage-keyed rules cache. `predictions/management/commands/update_points.py` is the equivalent manual/backfill sweep over `is_processed=False` predictions (or all, with `--all`).
5. **Leaderboard display** (`discord_bot/cogs/leaderboard_sync.py`): polls every 30s, diffs a fingerprint (leaderboard + rules tuple) against the last-rendered state per `DiscordGuildPool` to avoid needless Discord API calls, and edits a single pinned message. Above rank 10 it collapses remaining users into a "Plebs" field, splitting across multiple fields if the 1024-char Discord field limit would be exceeded ("Pleb-Splitting").
6. **Garbage cleanup** (`discord_bot/cogs/remove_garbage.py`): deletes Discord's auto-generated "poll results are in" system messages both live (`on_message`) and via a startup historical sweep over all `ActiveMatchMessage` threads.

`discord_bot/cogs/match_ticker.py` (`MatchTickerCog`) exists but is currently commented out of `setup_hook` in `discord_bot/bot.py` — check whether it's expected to be wired in before assuming it runs.

### Conventions worth knowing

- Async-first: the bot and ingestion code use Django's async ORM (`aget`, `acreate`, `aiterator`, `aupdate_or_create`, etc.) throughout since discord.py's event loop can't block on sync ORM calls. Management commands that need to call async ingestion code wrap it in `asyncio.run(...)`.
- Model methods that touch scoring (`Prediction.update_points`/`aupdate_points`) are duplicated in sync and async form intentionally — signals call the sync path, the bot/async commands call the async path.
- `ExternalMappingBase` is the abstract base for all `*Mapping` models (`provider` + `external_id`, unique together); new provider integrations should extend `SportsProvider` and follow the same mapping-table pattern rather than adding provider-specific fields to core models.
- `REDIS_URL` doubles as both the Django cache backend (`django_redis`) and, via `CELERY_BROKER_URL`, the Celery broker default — don't assume they're separate Redis instances unless `.env` overrides them.
- Discord IDs are stored as `BigIntegerField` primary keys (not Django auto-increment) across `discord_bot` models — always pass the real Discord snowflake as `id=`.
