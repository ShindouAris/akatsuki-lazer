# akatsuki-lazer

A server backend for the osu! lazer client, written in Python (FastAPI) with a Go service for realtime multiplayer.
It is meant to be used with a modified client: <https://github.com/ShindouAris/osu>.

```
 osu! lazer client
   |  HTTPS  :8000   REST /api/v2, OAuth, /spectator, /metadata   -> app        (Python, FastAPI)
   |  HTTPS  :8081   /multiplayer (SignalR + MessagePack)         -> multiplayer (Go)
                                                                       |  /internal/*  (X-Internal-Key)
                                                                       v
                                                    app  <->  PostgreSQL + Redis
```

| Part | Where | Does |
|---|---|---|
| `app` | `app/` | REST API (`/api/v2`), OAuth2/JWT, scores, beatmaps, chat, spectator and metadata hubs, internal API |
| `multiplayer` | `multiplayer/` | rooms, ranked play, matchmaking, duels; stores everything through the app's internal API |
| `seed` | `app/seeders/` | fills the matchmaking pools with ranked beatmaps |

## What works

**Players and social:** registration, login (OAuth2 password and refresh tokens), profiles and statistics for all
modes, friends and blocks, chat channels and presence, notifications (REST), live rank lookup, rank history.

**Beatmaps and scores:** lookup by id, checksum or filename, beatmapset search and download (from a mirror or the
official osu! API), cover images, score tokens and submission, score download, PP calculation, beatmap
leaderboards (one row per player, best score first).

**Spectating:** the `/spectator` hub including the V2 targets (`BeginPlaySessionV2`, `SendFrameDataV2`,
`EndPlaySessionV2`) the current client uses, with frame buffering in Redis for late watchers.

**Multiplayer (Go service):**
- Lobby and rooms: create, join (with password), host and queue modes, playlist items, mods, ready/start/abort,
  skip votes, kick, host transfer, invitations.
- Head-to-head and **team versus** rooms (players are split between the red and blue team and can switch).
- **Start timers:** the host's "Start in..." button and per-room auto start.
- Room scores and leaderboards (`/api/v2/rooms/{id}/playlist/{item}/scores`) and the lobby list
  (`GET /api/v2/rooms?category=realtime`).
- **Ranked play and matchmaking:** pools (`1v1 Nomod`, `1v1 RX`, `1v1 AP`), rating-based pairing (OpenSkill),
  duels, card hands, discard/play stages and damage rules.

**Not implemented:** forums, comments, beatmap discussions, news, wiki, score pins, spotlights, teams (site
feature), real-time notifications over WebSocket, scoring per team in team versus results, free-style rooms.
Real-client testing is manual: expect rough edges when two clients play ranked play.

## Requirements

- Docker with Compose (the supported way to run everything), **or**
- Python 3.11+, [uv](https://docs.astral.sh/uv/), PostgreSQL (SQLite works for tests), Redis, Go 1.23+ for the
  multiplayer service.

## Quick start (Docker)

The client only talks HTTPS, so the stack runs behind a development certificate authority.

```bash
cp .env.example .env            # then edit it: SECRET_KEY, OAUTH_CLIENT_SECRET, INTERNAL_API_KEY (required)

# JWT signing keys: the app reads cert/private.pem and cert/public.pem (it does not create them), and the Go
# service verifies tokens with public.pem.
mkdir -p cert
openssl genrsa -out cert/private.pem 2048
openssl rsa -in cert/private.pem -pubout -out cert/public.pem

# development certificates into ./dev-certs (git-ignored; contains private keys)
# (-keep-ca issues a new server certificate from the existing authority, -force replaces the authority too)
cd multiplayer && go run ./cmd/devcert && cd ..
# trust the CA for your user (Windows; a confirmation dialog appears)
certutil -user -addstore Root "dev-certs\ca.pem"

docker compose up -d --build    # postgres, redis, app (migrates on start), multiplayer
docker compose run --rm seed    # once: matchmaking pools and their beatmaps (needs the beatmap mirror)
```

- API: `https://localhost:8000/api/v2/` (docs at `/docs`)
- Spectator and metadata hubs: `https://localhost:8000/spectator`, `/metadata`
- Multiplayer hub: `https://localhost:8081/multiplayer`
- PostgreSQL `127.0.0.1:3333`, Redis `127.0.0.1:9090` for tools on the host.

Rebuilding `app` or `multiplayer` (`docker compose up -d --build <service>`) drops connected clients and closes
open rooms; do not do it in the middle of a test. `seed` has its own image: after changing seeder code run
`docker compose --profile tools build seed`.

More detail, the client settings table and what was and was not checked: [`docs/LOCAL_CLIENT_TESTING.md`](docs/LOCAL_CLIENT_TESTING.md).

## Configuration

Settings come from environment variables or `.env` (see `.env.example` for every key):

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` (SQLite `sqlite+aiosqlite:///./osu.db` for quick local runs) |
| `REDIS_URL` | Redis, required for caching and the spectator frame buffer |
| `SECRET_KEY` | change in production (`openssl rand -hex 32`) |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` | must match the client's endpoint configuration |
| `API_BASE_URL` / `WEBSITE_URL` | use `https://` when testing with the client |
| `USE_BEATMAP_MIRROR`, `BEATMAP_MIRROR_URL` | beatmap source; with the mirror off, `OSU_API_CLIENT_ID/SECRET` are used |
| `INTERNAL_API_KEY` | enables `/internal/*`; must equal the Go service's key. Empty disables the internal API |
| `USE_EXTERNAL_MULTIPLAYER` | `true` when the Go service serves `/multiplayer` so this process does not |

The Go service's own variables (`LISTEN_ADDR`, `PYTHON_API_URL`, `API_CA_FILE`, `JWT_PUBLIC_KEY_FILE`, ...) are
documented in [`multiplayer/README.md`](multiplayer/README.md); `docker-compose.yml` sets them for you.

## Running without Docker

```bash
uv sync                                   # or: python -m venv .venv && pip install -e ".[dev]"
alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --ssl-keyfile dev-certs/localhost-key.pem --ssl-certfile dev-certs/localhost.pem

cd multiplayer && go run ./cmd/multiplayer      # needs the environment from multiplayer/README.md
python -m app.seeders matchmaking               # matchmaking pools
```

`scripts/dev.ps1` automates this on Windows (containers for PostgreSQL/Redis, migrations, seed, two windows).

## Connecting the client

Run the modified client, then Settings -> Debug -> Server:

| Setting | Value |
|---|---|
| Use custom server | on |
| Server URL | `https://localhost:8000` |
| Spectator / Metadata socket URL | `https://localhost:8000` (the client appends the path) |
| Multiplayer socket URL | `https://localhost:8081` |
| Client ID / secret | `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` from `.env` |

> [!IMPORTANT]
> Everything must be served over HTTPS and the development CA must be trusted, otherwise the client cannot
> reach the hubs.

## Tests

```bash
uv run pytest                # Python (SQLite, no services needed)
cd multiplayer && go test ./...   # Go (no external services needed)
```

Known failing tests that predate the multiplayer work: `tests/test_api.py::test_add_friend_success` and
`::test_get_user_score_rank`.

## Layout

```
app/api/v2/        REST endpoints compatible with the client
app/api/hubs/      SignalR hubs (spectator, metadata; multiplayer when not external)
app/api/internal/  internal API used by the Go service (rooms, matchmaking, scores)
app/models/        SQLAlchemy models        alembic/   migrations
app/services/      beatmaps, scores, ranks, leaderboard, chat, hub state
app/seeders/       matchmaking pool seeder  (sets listed in matchmaking_sets.txt)
multiplayer/       Go realtime service      docs/      local testing guide
```
