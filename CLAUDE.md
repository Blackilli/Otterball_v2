# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Otterball v2 is a Django + Discord bot sports prediction platform (originally built around the FIFA World Cup, now also running NFL pools). Users submit match predictions via native Discord polls; a Celery cluster ingests match data from the FIFA API, computes points, and a bot posts/reconciles polls and a live leaderboard in Discord. The Django web side is intentionally thin — it exists almost entirely to serve `/admin/` (data management) and `/media/` (team logos); there are no real user-facing views (`sports/views.py`, `predictions/views.py`, `users/views.py`, `discord_bot/views.py` are all empty stubs).

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

uv run python manage.py sync_fifa_infra --sync-teams --sync-competitions ...   # FIFA backfills, one flag per step
uv run python manage.py sync_nfl_infra                       # NFL: full setup for the current season (skeleton + teams + schedule)
uv run python manage.py sync_nfl_infra --season 2026 --sync-upcoming-matches --lookahead-days 25
uv run python manage.py sync_nfl_infra --sync-live-matches   # pull status/scores for matches that are live or already due

uv run python manage.py create_pool --name "NFL 2026" --sport AMERICAN_FOOTBALL --year 2026   # pool + config + stage rules (idempotent)
uv run python manage.py check_pool                           # is a pool actually ready? exits non-zero on FAIL

uv run python manage.py export_db [output.json.gz]    # clean full DB export (natural keys, skips contenttypes/permissions/sessions/celery results)
uv run python manage.py import_db backups/foo.json.gz --flush   # restore a dump; --flush wipes existing rows first to avoid PK conflicts
```

Local Docker stack: `docker compose up -d --build` (services: `web`, `bot`, `worker`, `beat`, `db` (Postgres 18), `valkey` (Redis-compatible cache/broker)). All four app services (`web`, `bot`, `worker`, `beat`) build from the same image/Dockerfile and just run different commands — keep that in mind when a change needs to reach all of them.

Pre-commit hooks (`.pre-commit-config.yaml`) run pyupgrade, django-upgrade, yamlfix, black, isort, `uv lock`/`uv sync`, the full Django test suite, and gitleaks — expect commits to be slow/blocked if these fail. Formatting: black/isort with `line_length = 118`.

## Architecture

Four Django apps, each with a distinct responsibility:

- **`sports`** — the sport data domain: `Competition` → `Season` → `Stage` → `Match`, plus `Team`. Every one of these has a parallel `*Mapping` model (`CompetitionMapping`, `SeasonMapping`, `StageMapping`, `TeamMapping`, `MatchMapping`) that links a local row to an external provider's ID (`SportsProvider.FIFA` for soccer; `SportsProvider.ESPN` and `SportsProvider.NFLVERSE` for the NFL, which is deliberately dual-sourced). This mapping-table pattern is how `sports/services/ingestion.py` upserts data idempotently from `sports/integrations/fifa.py` (the FIFA API client) without duplicating rows — always look up/create via the mapping table, not by guessing a match on name/date. `sports/integrations/fifa.py`'s pydantic schemas model the FIFA Digital Core Platform API, documented at `https://api.fifa.com/ApiFdcpSwagger/docs/v1` (Swagger 2.0 JSON, no auth required, ~260 paths) — treat that as the ground truth over the schema code when in doubt, since field aliases here are hand-transcribed and have drifted from the real API before (British-vs-American spelling, single-vs-double `s`, missing fields entirely — see `sports/tests/__init__.py::RealFifaApiResponseTests`). `sports/integrations/espn.py` is the NFL counterpart and spans two ESPN hosts on purpose: `sports.core.api.espn.com` for the season skeleton and schedule (its payloads nest entities as `{"$ref": ...}` links, and the ids we need are parsed out of those URLs by `_ref_segment` rather than fetched — chasing them would cost four extra requests per game) and `site.web.api.espn.com/apis/fantasy/v2/.../games` for status + scores of a whole date range in one request. A core `Event.id` **is** the scoreboard's `competitionId`, so a single `MatchMapping` serves both and no fuzzy matching is needed. Note `site.api.espn.com` — the host the old Otterball-NFL bot used — now returns 403 (Akamai), so don't reach for it. Neither ESPN API is documented; `sports/tests/test_espn.py::RealEspnApiResponseTests` pins the keys ingestion depends on (it asserts *required keys are still present* rather than FIFA's stricter *zero unmapped keys*, since ESPN ships far more per payload than is worth modelling).
`sports/integrations/nflverse.py` is the NFL's second provider: one CSV asset on a GitHub release (`nflverse-data/releases/download/schedules/games.csv`, no auth), read with stdlib `csv` rather than through `nfl_data_py` — the *package* pins numpy/pandas and stops at Python 3.12, but the *data* is just a file. It covers the schedule and **final** results back to 1999 and is a batch export, so it never carries in-progress state; ESPN stays the live source. Its `game_type` column already uses `REG`/`WC`/`DIV`/`CON`/`SB`, matching `NFL_STAGE_BLUEPRINTS`, and its `espn` column carries the matching ESPN event id for every row (verified 100% for 2025 and 2026) — a free cross-reference, which is why `ingest_nflverse_nfl_matches` attaches **both** mappings and adopts an existing ESPN-ingested match rather than duplicating it.
- **`predictions`** — `PredictionPool` (a competition instance users play in) → `PoolConfiguration` (when/how polls get created) and `PoolStageRule` (points per correct pick, keyed by stage, with a `stage=None` row as the pool-wide fallback, and `3` as the final hardcoded fallback). `Prediction` ties a `User` + `Match` + `PredictionPool` to a predicted `MatchOutcome`, and knows how to score itself (`update_points`/`aupdate_points`, sync and async variants — keep both in sync if you touch the scoring logic). `PredictionPool.aget_leaderboard()` implements Standard Competition Ranking (1-2-2-4): ties share a rank and the next rank skips accordingly.
- **`discord_bot`** — mirrors real Discord entities (`DiscordGuild`, `DiscordChannel`, `DiscordGuildRole`) plus app-specific state: `DiscordProfile` (1:1 with `users.User`), `ActiveMatchMessage` (the live link between a `Match`, a guild's poll thread/message, and its leaderboard/ticker message state), `DiscordGuildPool` (which pool is active in which guild/channel, with an optional notification role and pinned leaderboard message), and `DiscordTeamEmoji` (custom emoji per team, used on poll buttons/leaderboard).
- **`users`** — thin `AbstractUser` subclass with a `uuid` field and an `is_discord_linked` convenience property.

### Data flow

1. **Ingestion** (`sports/tasks.py`, scheduled via Celery Beat / `django_celery_beat`): `sync_daily_infrastructure` walks competitions → national teams → seasons → stages → upcoming matches; `sync_live_games` polls only matches that are `LIVE` or `SCHEDULED` within 15 minutes of kickoff. All ingestion functions are async and live in `sports/services/ingestion.py`; they build local dicts as caches (`*_mapping_cache`) before looping to avoid N+1 queries against Postgres — follow this pattern rather than querying inside a loop. `sports/management/commands/sync_fifa_infra.py` exposes each step as a manual `--sync-*` CLI flag for backfills. The NFL path is `sync_nfl_infrastructure` (competition/season/rounds → teams → schedule) and `sync_nfl_live_games`, with `sync_nfl_infra.py` as the CLI equivalent. **Every `ingest_*` function is `async def`** — Celery tasks are sync, so they must wrap each call in `asyncio.run(...)` (via the `_run` helper in `sports/tasks.py`); calling one bare just builds a coroutine and silently discards it, which is exactly how `sync_daily_infrastructure`/`sync_live_games` were no-ops until this was fixed.
2. **Poll creation** (`discord_bot/cogs/poll_creation.py`, `PollCreationCog`): a self-adjusting `tasks.loop` that re-reads all active pools' configured `poll_creation_time`s from the DB every minute (`interval_sync_loop`) and reconfigures the actual posting loop's fire times (`poll_creation_loop`) accordingly — new/changed `PoolConfiguration` rows take effect without a bot restart. Poll answer ordering (draw included or not) is decided by `Stage.stage_type` via `DISCORD_POLL_ANSWER_ORDER_MAP` in `discord_bot/constants.py` — `GROUP` and `LEAGUE` (soccer groups, NFL regular season) get the 3-answer drawable ordering, `KNOCK_OUT` gets 2. A stage type absent from that map makes poll creation log an error and **skip the match entirely**, so any new stage type must be added there; `OTHER` is left out deliberately so an unclassified stage fails loudly.
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
- **NFL rounds are modelled one `Stage` each, not one per week.** `NFL_STAGE_BLUEPRINTS` in `sports/constants.py` defines the five scoreable rounds — Regular Season (`LEAGUE`, level 0), Wild Card, Divisional, Conference Championship, Super Bowl (all `KNOCK_OUT`, levels 1-4) — so a pool sets points per round with five `PoolStageRule` rows, the same five knobs the old Otterball-NFL bot's `GameTypeScaling` had. `nfl_stage_key(season_type, week)` maps an ESPN event onto one, and returns `None` for preseason, the offseason, and **postseason week 4 (the Pro Bowl)**, which are filtered out of ingestion rather than turned into polls. `StageMapping.external_id` is `"{year}:{key}"`, e.g. `2026:SB`.
- The NFL regular season is the only round that offers a Draw poll answer, because it is the only one where a tie is possible. That is why its `stage_type` is `LEAGUE` and the playoff rounds are `KNOCK_OUT`.
- ESPN reports an unplayed game's score as `""` (empty string), not `null`. `ScoreboardCompetitor.parse_score` maps it to `None` — do not coerce it to `0`, or an unplayed game looks like a 0-0 draw and scores points.
- `ingest_espn_nfl_matches` owns kickoff/teams/stage only; `ingest_espn_nfl_live_matches` owns status and scores. Keep that split — the live path is what transitions a match to `FINISHED`, which is what fires the scoring signal, and a schedule re-run must never walk a finished match back to `SCHEDULED`.
- **nflverse and ESPN disagree on two team abbreviations**: nflverse says `LA`/`WAS`, ESPN says `LAR`/`WSH` (the other 30 match). `NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS` bridges them, and `ingest_nflverse_team_mappings` seeds a `TeamMapping` per team keyed by nflverse's abbreviation. Without it the Rams and Commanders never resolve and roughly two games a week are silently dropped — run the team sync before the nflverse sync on a fresh database, or every game is skipped as unmapped. Both syncs guard against this rather than failing quietly: the seeder logs an error if it ends up with fewer than `NFL_TEAM_COUNT` mappings, and the match ingest collects unresolved abbreviations and reports them **once** with a game count (`Unmapped nflverse team abbreviations: LA (17 games)`) instead of one near-identical line per game. Unmapped teams and missing stages are reported separately, because they need different fixes — the team sync vs the infrastructure sync.
- nflverse's `result` column is home-minus-away, so `0` means a tie — test `is not None`, never truthiness, or every tied game reads as unplayed. Likewise a blank score cell means "not played yet" and must stay `None`, not become `0`.
- `sports/tests/fixtures/*.json` are genuine captured responses from the live FIFA API (World Cup 2026, `competition_id=17`, `season_id=285023`), fetched via `curl -A "Mozilla/5.0" "https://api.fifa.com/api/v3/..."`. `RealFifaApiResponseTests` in `sports/tests/__init__.py` parses them and asserts every JSON key maps to a model field — parsing success alone isn't enough, since pydantic's default `extra="ignore"` silently drops unmapped keys instead of erroring. Refresh these fixtures periodically (or when touching `fifa.py`'s schemas) to catch further drift from the real API.

## Starting a new pool

The bot is generic over sports; standing up a pool is one ingestion run plus one command.

```bash
# 1. Sport data: competition, season, rounds, teams (logos + colours), schedule.
uv run python manage.py sync_nfl_infra

# 2. The pool itself: PredictionPool + PoolConfiguration + one PoolStageRule per round.
uv run python manage.py create_pool --name "NFL 2026" --sport AMERICAN_FOOTBALL --year 2026 \
    --weekdays 2 --time 18:00 --lookahead 7 \
    --points "Regular Season=1,Wild Card=2,Divisional=3,Conference Championship=4,Super Bowl=5"

# 3. Start the bot once. ReconciliationCog creates the DiscordGuild/Channel/Role rows
#    and EmojiSyncCog registers a team emoji per logo.
uv run python manage.py runbot

# 4. Bind the pool to a guild + channel (now that step 3 has populated them).
uv run python manage.py create_pool --name "NFL 2026" --sport AMERICAN_FOOTBALL --year 2026 \
    --guild <guild_id> --channel <channel_id> --notification-role <role_id>

# 5. Confirm it is actually ready.
uv run python manage.py check_pool
```

Then schedule the ingestion. **Celery Beat is database-driven** (`CELERY_BEAT_SCHEDULER =
django_celery_beat.schedulers:DatabaseScheduler`), so a task existing in `sports/tasks.py` does
nothing until a `PeriodicTask` row exists for it in `/admin/django_celery_beat/periodictask/`.
For the NFL that means three rows:

| Task | Suggested cadence | Does |
|---|---|---|
| `sports.tasks.sync_nfl_infrastructure` | daily | season skeleton, teams, abbreviation bridge, schedule |
| `sports.tasks.sync_nfl_live_games` | every ~2 min | status + scores; this is what transitions a match to `FINISHED` and fires scoring |
| `sports.tasks.sync_nflverse_results` | a few times a day | independent backstop for finals, plus ESPN id cross-referencing |

`create_pool` is idempotent — re-running with the same `--name` and season updates rather than
duplicating, which is why steps 2 and 4 are the same command. Everything it does is also available
in `/admin/`: `PredictionPool` carries `PoolConfiguration` and `PoolStageRule` as inlines and seeds
missing rules on save, and `DiscordGuildPool` is where the guild binding lives.

**The ordering in step 3 is a hard dependency, not a preference.** `DiscordGuild`, `DiscordChannel`
and `DiscordGuildRole` rows are only created by the bot's `ReconciliationCog` on `on_ready`, so
until the bot has connected once there is nothing to bind a pool to — the admin dropdowns are empty
and `create_pool --guild` refuses with a message saying so.

### Why check_pool exists

Almost every way a pool can be misconfigured is silent:

| Misconfiguration | Symptom without the check |
|---|---|
| No `PoolStageRule` rows | Every pick scores the hardcoded fallback of `3`; per-round scaling silently doesn't happen |
| Stage type missing from `DISCORD_POLL_ANSWER_ORDER_MAP` | Poll creation skips every match in that round |
| No `DiscordGuildPool`, or one with a null channel | The pool never posts anything at all |
| Empty `poll_creation_weekdays` | Polls never fire |
| No matches ingested in the lookahead window | Poll creation runs and finds nothing |

`check_pool` reports each as OK/WARN/FAIL and exits non-zero on FAIL, so it also works as a deploy gate.

### Which NFL provider owns what

| | ESPN | nflverse |
|---|---|---|
| Schedule | ✅ forward window | ✅ whole season |
| In-progress scores | ✅ (`sync_nfl_live_games`, every ~2 min) | ❌ batch only |
| Final results | ✅ | ✅ (independent backstop) |
| Cadence | live loop + daily | a few times a day (`sync_nflverse_results`) |

`ingest_nflverse_nfl_matches` only writes status/scores when nflverse reports a game as played *and* the match is not already `FINISHED`, so it settles anything ESPN missed without re-firing the scoring signal for matches ESPN already closed.
