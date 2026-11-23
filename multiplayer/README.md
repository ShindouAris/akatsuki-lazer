# multiplayer (Go)

Realtime multiplayer hub for the osu! lazer client, replacing `app/api/hubs/multiplayer.py`.
It speaks the SignalR **MessagePack** protocol over WebSocket and is a port of the room logic in
`osu-server-spectator` (`ServerMultiplayerRoom`, `StandardMatchController`, `MultiplayerHub`).

```
go test ./...          # no external services needed
go run ./cmd/multiplayer
```

| Variable | Default | Meaning |
|---|---|---|
| `LISTEN_ADDR` | `:8081` | listen address |
| `JWT_PUBLIC_KEY_FILE` | `../cert/public.pem` | public key of the Python API (tokens are RS256) |
| `JWT_AUDIENCE` | `5` | must equal `OAUTH_CLIENT_ID` |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | – | serve HTTPS (the client requires it) |

Endpoints: `POST /multiplayer/negotiate`, `GET /multiplayer` (WebSocket), `GET /health`.
Point the client's `MultiplayerUrl` at this service (or route `/multiplayer*` to it from a reverse proxy).

## Layout

| Package | Responsibility |
|---|---|
| `internal/protocol` | wire types. Structs encode as MessagePack arrays in the client's `[Key(n)]` order; decoding tolerates longer/shorter arrays like MessagePack-CSharp |
| `internal/signalr` | handshake, length-prefixed framing, completions, keep-alive, per-connection send queue |
| `internal/auth` | RS256 token verification (`sub` = user id, audience, `type=access`) |
| `internal/room` | the room state machine and manager; one mutex per room |
| `internal/hub` | maps hub method names to room operations |

## Implemented

`CreateRoom`, `JoinRoom`, `JoinRoomWithPassword`, `LeaveRoom`, `TransferHost`, `KickUser`, `ChangeSettings`,
`ChangeState`, `ChangeBeatmapAvailability`, `ChangeUserMods`, `StartMatch`, `AbortMatch`, `AbortGameplay`,
`VoteToSkipIntro`, `AddPlaylistItem`, `EditPlaylistItem`, `RemovePlaylistItem`.

Behaviour that follows the reference server: state transition rules and their exact error texts, ignoring
late gameplay-state messages instead of erroring, the 30 s load timeout, majority skip votes, host hand-over
when the host leaves, per-user playlist limits (3 guest / 50 host), host-only requeue after each round,
round-robin queue ordering, unreadying players when settings or the current item change.

## Ranked play (the match itself)

A port of `RankedPlayMatchController` and its ten stages (`internal/room/rankedplay*.go`): hands of 5 cards, the
discard phase, the active player's turn, waiting for the beatmap and for both players to be ready, gameplay,
damage from the score difference (room and per-player multipliers, 50,000 base damage, no-show damage), the
turn order, forfeits, and the end of the match. Hub methods: `DiscardCards`, `PlayCard` and `SendMatchRequest`
(card hand replays). Stage timers are real countdowns, announced to clients as `MatchEvent`s and included in room
snapshots (`internal/room/countdown.go`).

Wire types are in `internal/protocol/rankedplay.go` and are tested against bytes the client itself produces
(`testdata/rankedplay_vectors.tsv`).

`Manager.CreateRankedPlay` creates such a room; clients cannot (`CreateRoom` accepts head-to-head and team versus).

## Team versus and start timers

Team versus rooms (`internal/room/teamversus.go`) have a red and a blue team: a joining player gets the smaller
team, `ChangeTeamRequest` (sent with `SendMatchRequest`) moves a player, and switching the room's match type
creates or removes the teams. Team scoring in the results is not implemented.

The match start timer (`internal/room/matchstart.go`): the host or a referee starts one with
`StartMatchCountdownRequest` ("Start in...") and stops it with `StopCountdownRequest`; a room with an auto start
duration starts one itself while someone is ready and stops it when nobody is. When it elapses the ready players
are sent to load gameplay.

## Matchmaking, duels and ratings

`internal/matchmaking` is a port of the reference server's queue: players wait in a pool, are paired by rating
(search radius grows with waiting time), invited for 30 s, and sent to a ranked play room made for them
(`MatchmakingRoomReady`). Decliners of a ranked queue are banned for a minute, recent opponents are kept apart,
cards are drawn around the lower rating. Duels (`MatchmakingIssueDuel` / `AcceptDuel`) skip the rating search and
are unranked: no Elo and no ban for leaving. The lobby (`MatchmakingJoinLobbyWithParams`) gets rating buckets.

Hub methods: `GetMatchmakingPoolsOfType`, `MatchmakingJoinLobbyWithParams`, `MatchmakingLeaveLobby`,
`MatchmakingJoinQueue`, `MatchmakingLeaveQueue`, `MatchmakingAcceptInvitation`, `MatchmakingDeclineInvitation`,
`MatchmakingIssueDuel`, `MatchmakingAcceptDuel`. They work only with `PYTHON_API_URL` set; without it they answer
"Matchmaking is not available".

Pools, cards, ratings and scores come from the Python API (`app/api/internal/matchmaking.py`, client in
`internal/store/matchmaking.go`); the result of a ranked match is recorded there once (idempotent) and rates the
players (OpenSkill Plackett-Luce, checked against OpenSkillSharp). Seed the pools `1v1 Nomod`, `1v1 RX`, `1v1 AP`
with `python -m app.seeders matchmaking`.

## Invitations

`InvitePlayer` sends the invited user an `Invited(by, roomId, password)` callback. Like the reference server it
refuses restricted users, blocks in either direction, and non-friends of players who take no messages from
non-friends (the client matches on those exact texts), and it is refused in ranked play rooms. The checks
come from `GET /internal/multiplayer/invites/{from}/{to}`; without `PYTHON_API_URL` they are skipped.

## Rank in the lobby

`UserRank` in the lobby status is 1 + the number of players with a higher rating in the pool, counting only
players who have finished a ranked match there; it is empty for everybody else.

## Chat (rooms only)

Go reports the users in a room (`participant_ids`) with every saved room state; Python makes the room's channel
(`channel_id`, shown as `#lazermp_<room id>`) open to exactly them (`app/services/chat.py`, REST `/chat/*`,
messages delivered on the notifications websocket after `chat.start`). There are no public channels and no private
messages. Not mutation-tested.

## NOT implemented (the client will see an error for these)

- `MatchmakingToggleSelection` and `MatchmakingSkipToNextStage` (the old quick play rooms; ranked play replaced
  them, so there are no quick play pools either), other `SendMatchRequest` kinds, `ChangeUserStyle` with a style
  (freestyle)
- the beatmap rating learning after a match
- team versus, referee hub, tournament mode, auto-start countdowns for ordinary rooms
- the ranked play gameplay load timeout is not announced as a `ForceGameplayStartCountdown` (it is enforced)
- match types other than head-to-head for client-created rooms: `CreateRoom` with another type is refused

## Persistence and the Go–Python contract

Python owns the database; Go owns live room state. Go never touches the database: it calls the Python API's
internal endpoints (`app/api/internal/multiplayer.py`, header `X-Internal-Key`).

| Go needs | Call | Behaviour |
|---|---|---|
| a room id and item ids | `POST /internal/multiplayer/rooms` | **blocking**; ids come from the database |
| a new playlist item | `POST /internal/multiplayer/rooms/{id}/items` | **blocking**; fails the user's request if it fails |
| room state changed | `PUT /internal/multiplayer/rooms/{id}` | write-behind, full state, latest wins |
| item changed / removed | `PUT` / `DELETE .../items/{item}` | write-behind, idempotent |
| beatmap checksum, mode, stars | `GET /internal/multiplayer/beatmaps/{id}` | cached for a minute |
| startup clean-up | `POST /internal/multiplayer/rooms/close-stale` | closes realtime rooms of a crashed run |

Why it works: score tokens, score linking (`multiplayer_scores`), the REST `/rooms` endpoints and the metadata
hub's room watching all read `multiplayer_rooms` / `multiplayer_playlist_items`, so rooms stored there are
visible to every existing Python feature with no extra event channel.

* Write-behind sends only what changed (the room diffs itself against what it last saved), coalesces updates
  per room/item, retries network errors and 5xx/429 with backoff, and drops 4xx answers (a resend cannot help).
  On shutdown every room is marked closed and the queue is flushed.
* `contract/*.json` are shared samples: the Go tests assert Go produces exactly them, the Python tests assert
  the models accept them and have the same field set. Changing a field on one side fails a test on the other.

Enable it:

```
# Python .env
INTERNAL_API_KEY=<long random value>
USE_EXTERNAL_MULTIPLAYER=true      # stop serving /multiplayer from Python

# Go service
PYTHON_API_URL=http://127.0.0.1:8000
INTERNAL_API_KEY=<same value>
```

Known limits of this design:

* An item edited in the lobby reaches the database a moment later (write-behind). A client asking for a score
  token for a just-edited item within that moment could be refused; in practice gameplay starts much later.
* If the API is down, room creation and adding items fail with "please try again"; everything else keeps
  working and catches up when it returns. A crash of the Go process loses writes still queued (milliseconds).
* `played_at` is stored in the database but sent to clients as null (the client's `DateTimeOffset` encoding is
  not verified yet).
* Restricted users and blocks are checked for invitations only, not for joining ordinary rooms.

## Verification status

Tested: wire format against bytes produced by the Python models, the client's captured `CreateRoom` payload,
full two-player match over a real WebSocket, JWT rejection cases (expired, wrong audience, refresh token,
forged, `alg: none`), reconnection, disconnect clean-up; persistence (what the store holds always equals what
clients see, retries, coalescing, failure handling) and the Go–Python contract (shared JSON samples). Several
tests were checked by mutation.

**Cross-language run:** `e2e/` drives the Go hub against the real Python internal router on SQLite and checks
the resulting rows (see `e2e/README.md`). It passed; it is not part of `go test ./...` because it needs the
Python environment.

**Not done:** a run against the real osu! lazer client (ranked play, matchmaking and chat are tested against
bytes the client produces, never against the client itself) and a run against PostgreSQL (SQLite only).

`go test -race ./...` needs a C compiler (cgo); it passes, and the room, matchmaking and hub packages also
passed 15 repeated runs under it.
