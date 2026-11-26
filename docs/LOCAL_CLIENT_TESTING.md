# Testing with the osu! lazer client on this machine

With `SSL=true` (the default) everything is served over HTTPS behind a development certificate authority. With
`SSL=false` the app and the hubs speak plain HTTP, for use behind nginx or a Cloudflare Tunnel (see the README).

## One-time setup

1. Certificates (git-ignored `dev-certs/`; to redo: `cd multiplayer; go run ./cmd/devcert -force`).
2. Trust the authority for your Windows user (a confirmation dialog appears):

   ```
   certutil -user -addstore Root "<repo>\dev-certs\ca.pem"
   ```

   Remove it later with `certutil -user -delstore Root "akatsuki-lazer development CA"`.
3. `.env` needs `INTERNAL_API_KEY`, `SSL=true` and https `API_BASE_URL` / `WEBSITE_URL`.
4. JWT keys in `cert/` (see the README quick start).

## Start everything

The whole stack in containers:

```
docker compose up -d --build
docker compose run --rm seed      # once: matchmaking pools (see the README for the ~10k-map discover seeder)
```

`.\scripts\dev.ps1 up` is the alternative on Windows: containers for Postgres (host port 3333) and Redis (9090),
migrations, seeding on first run, then the API and the Go multiplayer service in two windows. It does not start the
Go spectator and metadata services, so leave `USE_EXTERNAL_SPECTATOR` / `USE_EXTERNAL_METADATA` at `false` there.

## Client settings

Run the client with the fork, **sign out**, then Settings -> Debug -> Server:

| Setting | Value |
|---|---|
| Use custom server | on |
| Server URL | `https://localhost:8000` |
| Spectator socket URL | `https://localhost:8082` (the client adds `/spectator`) |
| Multiplayer socket URL | `https://localhost:8081` (the client adds `/multiplayer`) |
| Metadata socket URL | `https://localhost:8084` (the client adds `/metadata`) |
| Client ID | `OAUTH_CLIENT_ID` from `.env` (default `5`) |
| Client secret | `OAUTH_CLIENT_SECRET` from `.env` |

Press "restart to apply". Create accounts through registration in the client and log in with one on each of two
clients (or two Windows users) to try a duel or a queue.

User ids start at 2: the client ignores users with id 1 or below (id 0 is its system user).

## Tested and not tested

Tested by hand with real clients: login, multiplayer rooms, spectating, ranked play with two clients (queue,
pick phase, star rating decided, results) and the Cloudflare Tunnel setup. Everything else is listed in the
README under "Status". Expect rough edges when two clients play ranked play.
