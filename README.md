# akatsuki-lazer

A private-server backend for the osu! lazer client: a Python (FastAPI) API plus Go services for the realtime hubs
(multiplayer, spectator, metadata). It is built to be used with the modified client
<https://github.com/ShindouAris/osu>; an unmodified client needs extra work (see
[Connecting an unmodified client](#connecting-an-unmodified-osu-lazer-client)).

```
 osu! lazer client
   |  REST /api/v2, OAuth                                        -> app          (Python, FastAPI)  :8000
   |  /multiplayer  (SignalR + MessagePack)                      -> multiplayer  (Go)               :8081
   |  /spectator    (SignalR + MessagePack)                      -> spectator    (Go)               :8082
   |  /metadata     (SignalR + MessagePack)                      -> metadata     (Go)               :8084
                                                                       |  /internal/*  (X-Internal-Key)
                                                                       v
                                                    app  <->  PostgreSQL + Redis + SeaweedFS/S3
```

| Part | Where | Does |
|---|---|---|
| `app` | `app/` | REST API (`/api/v2`), OAuth2/JWT, scores, beatmaps, chat, internal API |
| `multiplayer` | `multiplayer/cmd/multiplayer` | rooms, ranked play, matchmaking, duels |
| `spectator` | `multiplayer/cmd/spectator` | live gameplay streaming, replay frame buffer in Redis |
| `metadata` | `multiplayer/cmd/metadata` | presence, daily challenge, beatmap updates |
| `seed` | `app/seeders/` | fills the matchmaking pools with ranked/loved beatmaps (metadata only) |

The three Go services are built from one image (`multiplayer/Dockerfile`) and store everything through the app's
internal API.

## Contents

1. [Quick start](#quick-start-docker)
2. [Connecting the modified client](#connecting-the-modified-client)
3. [Connecting an unmodified osu!lazer client](#connecting-an-unmodified-osu-lazer-client)
4. [Public hosting: TLS, nginx, Cloudflare Tunnel](#tls-and-public-hosting)
5. [Configuration](#configuration)
6. [Status: what works and what is missing](#status-what-works-and-what-is-missing)
7. [Storage, running without Docker, tests, layout](#where-replays-and-beatmaps-are-stored)

## Quick start (Docker)

```bash
cp .env.example .env            # edit: SECRET_KEY, OAUTH_CLIENT_SECRET, INTERNAL_API_KEY (required)

# JWT signing keys: the app reads cert/private.pem and cert/public.pem (it does not create them); the Go
# services verify tokens with public.pem.
mkdir -p cert
openssl genrsa -out cert/private.pem 2048
openssl rsa -in cert/private.pem -pubout -out cert/public.pem

# Local HTTPS (SSL=true, the default): development certificates into ./dev-certs (git-ignored, private keys!)
cd multiplayer && go run ./cmd/devcert && cd ..
certutil -user -addstore Root "dev-certs\ca.pem"      # Windows: trust the CA for your user

docker compose up -d --build    # postgres, redis, seaweedfs, app (migrates on start), multiplayer, spectator, metadata
docker compose run --rm seed    # once: matchmaking pools and their beatmaps (needs the beatmap mirror)
```

Optional, for a big pool (roughly 10,000 ranked and loved beatmaps, metadata only; files are fetched when a client
asks for them): run the discover seeder before the pool seeder.

```bash
docker compose run --rm seed python -m app.seeders matchmaking-discover matchmaking
```

- API `https://localhost:8000/api/v2/` (docs at `/docs`)
- Hubs `https://localhost:8081/multiplayer`, `:8082/spectator`, `:8084/metadata`
- PostgreSQL `127.0.0.1:3333`, Redis `127.0.0.1:9090`, SeaweedFS S3 `127.0.0.1:8333` for tools on the host.

Rebuilding a service drops connected clients and closes open rooms; do not do it in the middle of a test.
More detail: [`docs/LOCAL_CLIENT_TESTING.md`](docs/LOCAL_CLIENT_TESTING.md).

## Connecting the modified client

The fork <https://github.com/ShindouAris/osu> adds a custom-server section to the client. Run it, **sign out**, then
Settings -> Debug -> Server:

| Setting | Local (`SSL=true`) | Behind a tunnel / proxy (`SSL=false`, one hostname) |
|---|---|---|
| Use custom server | on | on |
| Server URL | `https://localhost:8000` | `https://lazer.example.com` |
| Metadata socket URL | `https://localhost:8084` | `https://lazer.example.com` |
| Spectator socket URL | `https://localhost:8082` | `https://lazer.example.com` |
| Multiplayer socket URL | `https://localhost:8081` | `https://lazer.example.com` |
| Client ID | `OAUTH_CLIENT_ID` (default `5`) | same |
| Client secret | `OAUTH_CLIENT_SECRET` from `.env` | same |

Press "restart to apply". The client appends `/multiplayer`, `/spectator` and `/metadata` itself. With a single
public hostname the ingress routes those three paths to the Go services (see below), so the three socket URLs can
equal the server URL.

For local testing the development CA must be trusted by the machine running the client, otherwise it cannot reach
the hubs. Test accounts can be created through registration in the client.

## Connecting an unmodified osu!lazer client

> [!WARNING]
> This path is **not tested** and the server does not fully support it yet.

The official client has no custom-server setting. It only knows ppy's endpoints; `osu! --devserver` switches it to
the development set (`https://dev.ppy.sh`, client id `5`, secret `3LP2mhUrV89xxzD1YKNndXHEhWWCRLPNKioZ9ymT`, hubs at
`https://dev.ppy.sh/signalr/{spectator,multiplayer,metadata}`). Pointing it at this server therefore needs:

1. a **hosts-file entry** sending `dev.ppy.sh` to the machine running the server;
2. a **TLS certificate for `dev.ppy.sh`** the client trusts (dev CA added to the trust store);
3. `OAUTH_CLIENT_ID=5` and `OAUTH_CLIENT_SECRET=3LP2mhUrV89xxzD1YKNndXHEhWWCRLPNKioZ9ymT` in `.env`;
4. a reverse proxy on port 443 that maps `/signalr/<hub>` to the hubs (stripping the `/signalr` prefix) and everything
   else to the app. **This server does not serve `/signalr/*` itself**, and Cloudflare Tunnel cannot rewrite paths,
   so use nginx for this case.

Features that rely on custom client code (queue pool picker tweaks, server-returned pp on the result screen, ...) are
absent in an unmodified client.

## TLS and public hosting

One switch in `.env` controls the app **and** the three Go hubs:

| `SSL` | Behaviour |
|---|---|
| `true` (default) | HTTPS with `dev-certs/localhost*.pem`; the hubs trust the dev CA to call the app |
| `false` | plain HTTP everywhere, no pem files needed; put TLS in front (nginx + certbot, Cloudflare Tunnel, ...) |

`app/main.py` also trusts `X-Forwarded-*` headers, so client IPs and schemes are correct behind a proxy. Cloudflare
sends the real client IP in `CF-Connecting-IP`.

**Cloudflare Tunnel (no open ports):** `cloudflared/config.yml` routes `/multiplayer`, `/spectator`, `/metadata` and
everything else on one hostname to the matching service over HTTP.

```bash
cloudflared tunnel login && cloudflared tunnel create akatsuki-lazer
cloudflared tunnel route dns akatsuki-lazer lazer.example.com
# copy the generated <id>.json to cloudflared/credentials.json (git-ignored); put <TUNNEL_ID> and your hostname
# into cloudflared/config.yml; set SSL=false in .env
docker compose --profile tunnel up -d --build
```

**nginx + certbot:** terminate TLS in nginx, proxy `/multiplayer` -> `:8081`, `/spectator` -> `:8082`,
`/metadata` -> `:8084`, `/` -> `:8000`; add `Upgrade`/`Connection` headers for WebSockets and
`proxy_read_timeout 3600s`. Do not publish ports 8081-8085 to the internet (8083/8085 are the internal APIs).

> [!NOTE]
> The Docker setup, both `SSL` modes and plain-HTTP hubs were verified to boot. A real tunnel or nginx in front of
> real clients has not been tested yet.

## Configuration

Settings come from environment variables or `.env` (see `.env.example` for every key):

| Variable | Meaning |
|---|---|
| `SSL` | `true`: HTTPS with the dev certificates; `false`: plain HTTP behind a TLS-terminating proxy (app and Go hubs) |
| `DATABASE_URL` | `postgresql+asyncpg://...` (SQLite `sqlite+aiosqlite:///./osu.db` for quick local runs) |
| `REDIS_URL` | Redis, required for caching, the spectator frame buffer and beatmap update queue |
| `SECRET_KEY` | change in production (`openssl rand -hex 32`) |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` | must match the client's endpoint configuration |
| `API_BASE_URL` / `WEBSITE_URL` | public URLs of the server; `https://` when the client talks to it |
| `USE_BEATMAP_MIRROR`, `BEATMAP_MIRROR_URL` | beatmap source; with the mirror off, `OSU_API_CLIENT_ID/SECRET` are used |
| `S3_*`, `SEAWEEDFS_*` | object storage for replays and beatmap files (see below) |
| `INTERNAL_API_KEY` | enables `/internal/*`; must equal the Go services' key. Empty disables the internal API |
| `USE_EXTERNAL_MULTIPLAYER` | `true` when the Go service serves `/multiplayer` |
| `USE_EXTERNAL_SPECTATOR`, `SPECTATOR_INTERNAL_URL` | Go spectator service (`http://spectator:8083`) |
| `USE_EXTERNAL_METADATA`, `METADATA_INTERNAL_URL` | Go metadata service (`http://metadata:8085`) |
| `PP_CAP_VANILLA/RELAX/AUTOPILOT` | most pp one play may award per board (0 = no cap) |

`docker-compose.yml` sets the `USE_EXTERNAL_*` flags and the Go services' own variables. Those are documented in
[`multiplayer/README.md`](multiplayer/README.md).

## Status: what works and what is missing

Legend: **Confirmed** = tried by the maintainer with real clients. **Implemented** = built and covered by tests
and/or checked against the API, but not yet confirmed in a real client.

### Confirmed with real clients

- Two modified clients in **ranked play**: queue, pairing, the **Pick Phase** (card hand) and the **star rating
  decided** screen.
- Login with a custom server, hubs over HTTPS with the dev CA, multiplayer rooms and spectating basics.

### Implemented (not yet confirmed in a client)

**Players and social:** registration, OAuth2 password + refresh tokens, profiles and statistics for all modes,
friends and blocks (country flag now returned correctly), chat channels and presence, notifications, live rank lookup,
rank history, batch user lookup (no more "Unknown user"), own flag/avatar/cover via API (see below).

**Beatmaps and scores:** lookup by id/checksum/filename, beatmapset search (the "Has Leaderboard" filter lists only
ranked, approved and loved maps), download from a mirror or the official API, cover images, score tokens and
submission, score and replay download (replay frames from the Go spectator are decoded), PP calculation, beatmap
leaderboards, **favouriting beatmapsets** (`POST/DELETE` favourites, `GET /me/beatmapset-favourites`).

**Boards (nomod / Relax / Autopilot):** a play goes to exactly one board by its mods (**RX** -> Relax in osu!,
taiko and catch; **AP** -> Autopilot in osu! only; anything else -> vanilla). Each board has its own leaderboard,
statistics, Redis pp ranking and rankings page. Endpoints take `?variant=vanilla|rx|ap`.

**Medals:** 84 medals with the rules of score-service-rs, awarded while a play is processed and pushed over the
notifications WebSocket.

**Spectating (Go service):** `/spectator` including the V2 targets, frame buffering for late watchers, replay
frames written for score replays.

**Metadata (Go service):** presence, friends online state, daily challenge, multiplayer room stats, beatmap
updates; the Python app forwards to it through the internal API.

**Multiplayer (Go service):** lobby and rooms (host/queue modes, playlist, mods, ready/start/abort, skip votes,
kick, host transfer, invitations), head-to-head and team versus, start timers, room scores and leaderboards.

**Ranked play and matchmaking:** OpenSkill-rated pairing, duels, card hands, discard/play stages and damage rules.
About 10,000 ranked and loved maps seeded as cards, **14 pools** (nomod, RX, AP and mod combinations such as RX HD,
RX DT, RX NC; see `DUEL_MODES` in `app/seeders/matchmaking_seeder.py`). Pool cards are cached in the Go service for a
minute. The result screen after a ranked match is faster (beatmap prefetch, PP and replay work off the event loop).

**Own flag, avatar and cover** (the client has no screen for this; `-k` skips the dev certificate check):

```bash
curl -k -X PUT    -H "Authorization: Bearer $TOKEN" -F "file=@avatar.png"  https://localhost:8000/api/v2/me/avatar   # PNG/JPEG/GIF/WebP, 2 MB
curl -k -X PUT    -H "Authorization: Bearer $TOKEN" -F "file=@cover.jpg"   https://localhost:8000/api/v2/me/cover    # 6 MB
curl -k -X PUT    -H "Authorization: Bearer $TOKEN" -F "country_code=JP"   https://localhost:8000/api/v2/me/country  # any 2 letters
curl -k -X DELETE -H "Authorization: Bearer $TOKEN" https://localhost:8000/api/v2/me/avatar                         # same for /cover
```

### Missing or incomplete

- Forums, comments, beatmap discussions, news, wiki, score pins, spotlights, site teams.
- Scoring per team in team versus results; free-style rooms.
- `has_favourited` is not part of the beatmapset JSON, so the client's favourite button does not show the current state.
- With 14 queue modes, matches are spread thin; trim `DUEL_MODES` if the queue is rarely filled.
- Unmodified-client support and a real-world tunnel/nginx deployment (see above) are untested.
- Real-client testing is manual: expect rough edges when two clients play ranked play.
- Two tests fail from before this work: `tests/test_api.py::test_add_friend_success` and `::test_get_user_score_rank`.

## Where replays and beatmaps are stored

Replays (`replays/<score id>.osr`) and beatmap files (`beatmaps/osz/<set>.osz`, `beatmaps/osu/<id>.osu`) are kept in
object storage (`app/services/storage.py`), with backends tried in this order:

1. **S3**, when `S3_BUCKET` is set (AWS, or any S3-compatible service via `S3_ENDPOINT_URL`);
2. **SeaweedFS**, when `SEAWEEDFS_ENDPOINT_URL` is set (a container in `docker-compose.yml`);
3. **the local disk** (`REPLAYS_PATH`, `BEATMAPS_PATH`), always the last resort.

A write goes to the first backend that accepts it, a read asks them in turn, and a backend that just failed is
skipped for 30 seconds. A file written to the fallback is **not** copied back to S3 when S3 returns. Move what is on
the local disk into object storage (safe to repeat, never deletes anything):

```bash
docker compose exec app python -m app.storage_migrate --dry-run
docker compose exec app python -m app.storage_migrate
```

## Running without Docker

```bash
uv sync                                   # or: python -m venv .venv && pip install -e ".[dev]"
alembic upgrade head
SSL=true python -m app.main               # reads .env; SSL=false for plain HTTP

cd multiplayer && go run ./cmd/multiplayer      # also ./cmd/spectator, ./cmd/metadata; see multiplayer/README.md
python -m app.seeders matchmaking               # matchmaking pools
```

`app/main.py` takes the certificate paths from `SSL_KEYFILE` / `SSL_CERTFILE` (default `certs/localhost*.pem`).
`scripts/dev.ps1` automates this on Windows.

## Tests

```bash
uv run pytest                     # Python (SQLite, no services needed)
cd multiplayer && go test ./...   # Go (no external services needed)
```

After upgrading an existing database (the migration sorts old scores into boards by their mods), recompute the
statistics per board and rebuild the Redis index once:

```bash
docker compose exec app python -m app.services.score_worker rebuild
```

## Layout

```
app/api/v2/        REST endpoints compatible with the client
app/api/hubs/      SignalR hubs (used only when the Go services are not external)
app/api/internal/  internal API used by the Go services (rooms, matchmaking, scores, spectator, metadata)
app/models/        SQLAlchemy models        alembic/   migrations
app/services/      beatmaps, scores, ranks, leaderboard, chat, hub state
app/seeders/       matchmaking pool seeders
multiplayer/       Go services (multiplayer, spectator, metadata)     cloudflared/  tunnel ingress
docs/              local testing guide
```
