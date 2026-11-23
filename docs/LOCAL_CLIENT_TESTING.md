# Testing with the osu! lazer client on this machine

The client only talks HTTPS, so everything runs behind a development certificate authority.

## One-time setup

1. Certificates (already generated in `dev-certs/`, git-ignored; to redo: `cd multiplayer; go run ./cmd/devcert -force`).
2. Trust the authority for your Windows user (a confirmation dialog appears):

   ```
   certutil -user -addstore Root "D:\Code\akatsuki-lazer\dev-certs\ca.pem"
   ```

   Remove it later with `certutil -user -delstore Root "akatsuki-lazer development CA"`.
3. `.env` needs `INTERNAL_API_KEY`, `USE_EXTERNAL_MULTIPLAYER=true` and https `API_BASE_URL` / `WEBSITE_URL`
   (already set here).

## Start everything

```
.\scripts\dev.ps1 up
```

Or run the whole stack in containers (what the README describes):

```
docker compose up -d --build
docker compose run --rm seed      # once: matchmaking pools (ranked beatmaps only, from the sets in app/seeders/matchmaking_sets.txt)
```

The script starts Postgres (host port 3333) and Redis (9090), migrates, seeds the matchmaking pools on first run
(`1v1 Nomod`, `1v1 RX`, `1v1 AP`; only ranked/approved osu beatmaps with a star rating are used as cards), then
opens two windows: the API on `https://localhost:8000` and the Go multiplayer service on
`https://localhost:8081`. Note: the script still names `docker-compose.dev.yml`, which is not in the repository;
use the `docker compose` commands above until the script is updated.

## Client settings

Run the client with your fork, then Settings -> Debug -> Server:

| Setting | Value |
|---|---|
| Use custom server | on |
| Server URL | `https://localhost:8000` |
| Spectator socket URL | `https://localhost:8000` (the client adds `/spectator`) |
| Multiplayer socket URL | `https://localhost:8081` (the client adds `/multiplayer`) |
| Metadata socket URL | `https://localhost:8000` (the client adds `/metadata`) |
| Client ID | `5` |
| Client secret | `OAUTH_CLIENT_SECRET` from `.env` |

Test accounts (created locally): `devplayer1` and `devplayer2`, password `devpass123`. Log in with one on
each of two clients (or two Windows users) to try a duel or a queue.

## What was checked, and what was not

Checked on this machine: migrations on PostgreSQL, seeding from the mirror, HTTPS on both servers, Go calling
the Python internal API over HTTPS with the development CA (`API_CA_FILE`), registering a user, an OAuth password
token, and the Go service accepting that token on `/multiplayer/negotiate`.

Not checked: the real client. Expect to find bugs when two clients play ranked play for the first time.
