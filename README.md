# py-lazer-server

A Python implementation of the osu! lazer server backend.

## Features

- **REST API** (`/api/v2/`) - Compatible with osu! lazer client
  - User authentication (OAuth2 with JWT tokens)
  - User profiles and statistics (all game modes)
  - Beatmap and beatmapset endpoints
  - Score submission and retrieval
  - Multiplayer room management
  - Friends and blocks (with database persistence)
  - Chat channels and messaging
  - Notifications with WebSocket support
  - Seasonal backgrounds

- **Real-time Hubs** (ASP.NET Core SignalR protocol)
  - `/spectator` - Live gameplay spectating and frame data
  - `/multiplayer` - Real-time multiplayer room coordination
  - `/metadata` - User presence tracking and online status
  - MessagePack binary serialization support
  - Variable-length integer (VarInt) message framing

## Requirements

- Python 3.11+
- SQLite (default) or PostgreSQL
- uv (recommended) or pip

## Installation

```bash
# Clone or navigate to the repository
cd py_lazer_server

# Using uv (recommended)
uv sync

# Or using pip
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
pip install -e ".[dev]"
```

## Configuration

The server uses environment variables or a `.env` file:

```env
# Server
DEBUG=true
HOST=0.0.0.0
PORT=8000

# Database
DATABASE_URL=sqlite+aiosqlite:///./osu.db
# or for PostgreSQL:
# DATABASE_URL=postgresql+asyncpg://user:pass@localhost/osu

# Security
SECRET_KEY=your-secret-key-here

# OAuth2 Client (must match osu! client configuration)
OAUTH_CLIENT_ID=5
OAUTH_CLIENT_SECRET=change-me
```

## Running

```bash
# Using uv
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# Or using python directly
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The server will be available at:
- API: `http://localhost:8000/api/v2/`
- SignalR hubs: `http://localhost:8000/spectator`, `/multiplayer`, `/metadata`
- API docs: `http://localhost:8000/docs`

## Connecting osu! lazer

To connect the osu! lazer client to this server:

1. Modify `osu.Game/Online/LocalEndpointConfiguration.cs`:
```csharp
public class LocalEndpointConfiguration : EndpointConfiguration
{
    public LocalEndpointConfiguration()
    {
        WebsiteUrl = APIUrl = @"http://localhost:8000";
        APIClientSecret = @"change-me";
        APIClientID = "5";
        SpectatorUrl = "http://localhost:8000/spectator";
        MultiplayerUrl = "http://localhost:8000/multiplayer";
        MetadataUrl = "http://localhost:8000/metadata";
    }
}
```

2. Update `OsuGameBase.cs` to use `LocalEndpointConfiguration`:
```csharp
public virtual EndpointConfiguration CreateEndpoints() => new LocalEndpointConfiguration();
```

3. For HTTP (non-HTTPS) connections, bypass SSL in `osu-framework`:
   - Add SSL bypass to `SocketsHttpHandler` in `WebRequest.cs`
   - Set environment variable: `OSU_INSECURE_REQUESTS=1`

4. Build and run the client:
```bash
export OSU_INSECURE_REQUESTS=1
dotnet build osu.Desktop.slnf
dotnet run --project osu.Desktop
```

## API Endpoints

### Authentication
- `POST /oauth/token` - Get access token (username/password grant)

### Current User
- `GET /api/v2/me` - Current user profile
- `GET /api/v2/me/{mode}` - Current user with mode-specific stats

### Users
- `GET /api/v2/users/{id}` - Get user by ID
- `GET /api/v2/users/{id}/{mode}` - Get user with mode-specific stats
- `GET /api/v2/users/lookup` - Lookup user by ID or username

### Friends & Blocks
- `GET /api/v2/friends` - List friends
- `POST /api/v2/friends` - Add friend
- `DELETE /api/v2/friends/{target_id}` - Remove friend
- `GET /api/v2/blocks` - List blocked users
- `POST /api/v2/blocks` - Block user
- `DELETE /api/v2/blocks/{target_id}` - Unblock user

### Chat
- `GET /api/v2/chat/channels` - List chat channels
- `GET /api/v2/chat/updates` - Get chat updates (polling)
- `POST /api/v2/chat/ack` - Acknowledge messages

### Notifications
- `GET /api/v2/notifications` - Get notifications (includes WebSocket endpoint)
- `POST /api/v2/notifications/mark-read` - Mark as read
- WebSocket: `/api/v2/notifications/websocket` - Real-time notifications

### Beatmaps
- `GET /api/v2/beatmaps/{id}` - Get beatmap
- `GET /api/v2/beatmaps/lookup` - Lookup by checksum/filename/id
- `GET /api/v2/beatmapsets/{id}` - Get beatmapset
- `GET /api/v2/beatmapsets/search` - Search beatmapsets

### Scores
- `POST /api/v2/beatmaps/{id}/solo/scores` - Request score token
- `PUT /api/v2/beatmaps/{id}/solo/scores/{token}` - Submit score
- `GET /api/v2/beatmaps/{id}/scores` - Get beatmap scores
- `GET /api/v2/users/{id}/scores/{type}` - Get user scores (best/recent/firsts)

### Multiplayer
- `GET /api/v2/rooms` - List rooms
- `POST /api/v2/rooms` - Create room
- `GET /api/v2/rooms/{id}` - Get room details
- `PUT /api/v2/rooms/{id}/users/{user_id}` - Join room
- `DELETE /api/v2/rooms/{id}/users/{user_id}` - Leave room

### Misc
- `GET /api/v2/seasonal-backgrounds` - Seasonal background images

## SignalR Hubs

The server implements ASP.NET Core SignalR protocol for real-time communication:

### Spectator Hub (`/spectator`)
- `BeginPlaySession` - Start spectating session
- `EndPlaySession` - End spectating session
- `SendFrameData` - Send gameplay frame data
- `StartWatchingUser` / `EndWatchingUser` - Watch/unwatch players

### Multiplayer Hub (`/multiplayer`)
- Room creation, joining, and state synchronization
- Playlist management
- Match start/end coordination

### Metadata Hub (`/metadata`)
- `BeginWatchingUserPresence` - Subscribe to online users
- `EndWatchingUserPresence` - Unsubscribe
- `UpdateActivity` - Update user activity
- `UpdateStatus` - Update online status
- `UserPresenceUpdated` - Broadcasts presence changes

## Project Structure

```
py_lazer_server/
├── app/
│   ├── api/
│   │   ├── v2/           # REST API endpoints
│   │   │   ├── oauth.py
│   │   │   ├── me.py
│   │   │   ├── users.py
│   │   │   ├── beatmaps.py
│   │   │   ├── scores.py
│   │   │   ├── rooms.py
│   │   │   ├── friends.py
│   │   │   ├── blocks.py
│   │   │   ├── chat.py
│   │   │   └── notifications.py
│   │   ├── signalr.py    # SignalR hub implementations
│   │   └── deps.py       # Dependency injection
│   ├── core/             # Configuration and utilities
│   │   ├── config.py
│   │   ├── database.py
│   │   └── security.py
│   ├── models/           # SQLAlchemy models
│   │   ├── user.py       # User, UserStatistics, UserRelation
│   │   ├── beatmap.py
│   │   ├── score.py
│   │   └── multiplayer.py
│   └── main.py           # Application entry point
├── tests/
├── pyproject.toml
└── README.md
```

## Database Models

- **User** - User accounts with profile information
- **UserStatistics** - Per-mode statistics (pp, rank, play count, etc.)
- **UserRelation** - Friends and blocks relationships
- **Beatmap** / **BeatmapSet** - Beatmap metadata
- **Score** / **ScoreToken** - Score submissions
- **MultiplayerRoom** / **MultiplayerPlaylistItem** - Multiplayer state

## License

MIT License - See LICENSE file for details.

## Acknowledgments

This implementation is based on the official osu! server infrastructure:
- [osu-web](https://github.com/ppy/osu-web) - REST API reference
- [osu-server-spectator](https://github.com/ppy/osu-server-spectator) - Real-time hub reference
- [osu](https://github.com/ppy/osu) - Client implementation reference
