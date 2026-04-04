"""API endpoint tests."""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_token_pair
from app.core.security import get_password_hash
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserRelation
from app.models.user import UserStatistics


@pytest.mark.asyncio
async def test_root(client: AsyncClient) -> None:
    """Test root endpoint."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "py-lazer-server"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    """Test health endpoint."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_oauth_token_missing_credentials(client: AsyncClient) -> None:
    """Test OAuth token endpoint with missing credentials."""
    response = await client.post(
        "/api/v2/oauth/token",
        data={"grant_type": "password"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_oauth_token_invalid_credentials(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test OAuth token endpoint with invalid credentials."""
    response = await client.post(
        "/api/v2/oauth/token",
        data={
            "grant_type": "password",
            "username": "nonexistent",
            "password": "wrongpassword",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oauth_token_valid_credentials(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test OAuth token endpoint with valid credentials."""
    # Create test user
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    # Add statistics
    for mode in GameMode:
        stats = UserStatistics(user_id=user.id, mode=mode)
        db_session.add(stats)
    await db_session.commit()

    response = await client.post(
        "/api/v2/oauth/token",
        data={
            "grant_type": "password",
            "username": "testuser",
            "password": "testpassword",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client: AsyncClient) -> None:
    """Test /me endpoint without authentication."""
    response = await client.get("/api/v2/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_authenticated(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test /me endpoint with authentication."""
    # Create test user
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    # Add statistics
    for mode in GameMode:
        stats = UserStatistics(user_id=user.id, mode=mode)
        db_session.add(stats)
    await db_session.commit()
    await db_session.refresh(user)

    # Create token
    token_pair = create_token_pair(user.id, ["*"])

    response = await client.get(
        "/api/v2/me",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert data["id"] == user.id
    assert "statistics" in data
    assert "grade_counts" in data["statistics"]


@pytest.mark.asyncio
async def test_get_user_not_found(client: AsyncClient) -> None:
    """Test user endpoint with non-existent user."""
    response = await client.get("/api/v2/users/99999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_user_by_id(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test getting user by ID."""
    # Create test user
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    for mode in GameMode:
        stats = UserStatistics(user_id=user.id, mode=mode)
        db_session.add(stats)
    await db_session.commit()
    await db_session.refresh(user)

    response = await client.get(f"/api/v2/users/{user.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert "statistics" in data
    assert "grade_counts" in data["statistics"]


@pytest.mark.asyncio
async def test_lookup_user_with_ids_bracket_trailing_slash(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Lookup endpoint should support ids[] with trailing slash requests."""
    user = User(
        username="lookupidsuser",
        email="lookupids@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    response = await client.get(
        "/api/v2/users/lookup/",
        params=[("ids[]", user.id)],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["users"][0]["id"] == user.id
    assert payload["users"][0]["username"] == user.username


@pytest.mark.asyncio
async def test_get_users_bulk_accepts_string_ids(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Bulk user lookup should accept ids[] values sent as strings."""
    first_user = User(
        username="bulkfirstuser",
        email="bulkfirst@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    second_user = User(
        username="bulkseconduser",
        email="bulksecond@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="GB",
    )
    db_session.add(first_user)
    db_session.add(second_user)
    await db_session.commit()
    await db_session.refresh(first_user)
    await db_session.refresh(second_user)

    response = await client.get(
        "/api/v2/users/",
        params=[("ids[]", str(second_user.id)), ("ids[]", first_user.username)],
    )

    assert response.status_code == 200
    payload = response.json()
    assert [user["id"] for user in payload["users"]] == [second_user.id, first_user.id]
    assert [user["username"] for user in payload["users"]] == [
        second_user.username,
        first_user.username,
    ]


@pytest.mark.asyncio
async def test_get_user_mode_invalid_ruleset_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Invalid ruleset short names should return 400 instead of silent osu fallback."""
    user = User(
        username="invalidmodeuser",
        email="invalidmode@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    for mode in GameMode:
        stats = UserStatistics(user_id=user.id, mode=mode)
        db_session.add(stats)
    await db_session.commit()

    response = await client.get(f"/api/v2/users/{user.id}/not-a-ruleset", params={"key": "id"})

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"] == "Invalid ruleset: not-a-ruleset"


@pytest.mark.asyncio
async def test_get_user_mode_by_id_returns_stats_payload(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Mode-specific user endpoint should return stats (including hit_accuracy) without server errors."""
    user = User(
        username="validmodeuser",
        email="validmode@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    for mode in GameMode:
        stats = UserStatistics(user_id=user.id, mode=mode)
        db_session.add(stats)
    await db_session.commit()

    response = await client.get(f"/api/v2/users/{user.id}/osu", params={"key": "id"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == user.id
    assert "statistics" in payload
    assert "hit_accuracy" in payload["statistics"]


@pytest.mark.asyncio
async def test_add_friend_success(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test adding a friend creates a friend relation and returns compact target user."""
    current_user = User(
        username="friendowner",
        email="friendowner@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    target_user = User(
        username="friendtarget",
        email="friendtarget@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="JP",
    )
    db_session.add(current_user)
    db_session.add(target_user)
    await db_session.commit()
    await db_session.refresh(current_user)
    await db_session.refresh(target_user)

    token_pair = create_token_pair(current_user.id, ["*"])
    response = await client.post(
        "/api/v2/friends",
        params={"target": target_user.id},
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["target_id"] == target_user.id
    assert data["relation_type"] == "friend"
    assert data["target"]["id"] == target_user.id

    relation = await db_session.get(UserRelation, (current_user.id, target_user.id))
    assert relation is not None
    assert relation.friend is True
    assert relation.foe is False


@pytest.mark.asyncio
async def test_register_user_success_root(client: AsyncClient) -> None:
    """Test registration endpoint at root path."""
    response = await client.post(
        "/users",
        data={
            "user[username]": "newuser",
            "user[user_email]": "newuser@example.com",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_register_user_success_v2(client: AsyncClient) -> None:
    """Test registration endpoint at versioned path."""
    response = await client.post(
        "/api/v2/users",
        data={
            "user[username]": "newuserv2",
            "user[user_email]": "newuserv2@example.com",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_register_user_duplicate_username(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test username duplication returns expected form_error shape."""
    existing = User(
        username="takenname",
        email="existing@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(existing)
    await db_session.flush()

    for mode in GameMode:
        db_session.add(UserStatistics(user_id=existing.id, mode=mode))
    await db_session.commit()

    response = await client.post(
        "/users",
        data={
            "user[username]": "takenname",
            "user[user_email]": "newmail@example.com",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "form_error": {
            "user": {
                "username": ["Username already taken"],
            }
        }
    }


@pytest.mark.asyncio
async def test_register_user_invalid_email(client: AsyncClient) -> None:
    """Test invalid email returns exact field error payload."""
    response = await client.post(
        "/users",
        data={
            "user[username]": "mailtest",
            "user[user_email]": "invalid-email",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "form_error": {
            "user": {
                "user_email": ["Email is invalid"],
            }
        }
    }


@pytest.mark.asyncio
async def test_register_user_short_password(client: AsyncClient) -> None:
    """Test short password returns exact field error payload."""
    response = await client.post(
        "/users",
        data={
            "user[username]": "pwdtest",
            "user[user_email]": "pwdtest@example.com",
            "user[password]": "123",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "form_error": {
            "user": {
                "password": ["Password too short"],
            }
        }
    }


@pytest.mark.asyncio
async def test_register_user_duplicate_email(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test duplicate email maps to expected field message."""
    existing = User(
        username="emailowner",
        email="dup@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(existing)
    await db_session.flush()

    for mode in GameMode:
        db_session.add(UserStatistics(user_id=existing.id, mode=mode))
    await db_session.commit()

    response = await client.post(
        "/users",
        data={
            "user[username]": "freshusername",
            "user[user_email]": "dup@example.com",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "form_error": {
            "user": {
                "user_email": ["Email is invalid"],
            }
        }
    }


@pytest.mark.asyncio
async def test_register_user_generic_fallback_error(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test generic error payload for unexpected registration failures."""

    async def failing_create_user(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.api.v2.users.create_user", failing_create_user)

    response = await client.post(
        "/users",
        data={
            "user[username]": "fallbackuser",
            "user[user_email]": "fallback@example.com",
            "user[password]": "strongpw",
        },
    )

    assert response.status_code == 500
    assert response.json() == {"error": "Something went wrong"}


@pytest.mark.asyncio
async def test_beatmapset_search_empty(client: AsyncClient) -> None:
    """Test beatmapset search with no results."""
    response = await client.get("/api/v2/beatmapsets/search")
    assert response.status_code == 200
    data = response.json()
    assert "beatmapsets" in data
    assert isinstance(data["beatmapsets"], list)


@pytest.mark.asyncio
async def test_get_beatmaps_by_ids_for_multiplayer(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Beatmaps list endpoint supports ids[] query format used by lazer."""
    beatmapset = BeatmapSet(
        user_id=None,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap_one = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Easy",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
    )
    beatmap_two = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Hard",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
    )
    db_session.add_all([beatmap_one, beatmap_two])
    await db_session.commit()

    response = await client.get(
        "/api/v2/beatmaps/",
        params=[
            ("ids[]", beatmap_two.id),
            ("ids[]", beatmap_one.id),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert "beatmaps" in data
    assert [beatmap["id"] for beatmap in data["beatmaps"]] == [beatmap_two.id, beatmap_one.id]
    assert data["beatmaps"][0]["beatmapset"]["id"] == beatmapset.id


@pytest.mark.asyncio
async def test_lookup_beatmap_by_checksum_and_filename(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Beatmap lookup endpoint accepts lazer checksum+filename requests."""
    beatmapset = BeatmapSet(
        user_id=None,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Insane",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
        checksum="94be7259e99197f6a06ca9d05607ed0e",
    )
    db_session.add(beatmap)
    await db_session.commit()

    response = await client.get(
        "/api/v2/beatmaps/lookup",
        params={
            "checksum": beatmap.checksum,
            "filename": "Pierce The Veil - Hell Above (Cut Ver.) (killian) [-digital's Extra].osu",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == beatmap.id
    assert data["checksum"] == beatmap.checksum
    assert data["beatmapset"]["id"] == beatmapset.id


@pytest.mark.asyncio
async def test_lookup_beatmapset_by_beatmap_id(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Beatmapset lookup endpoint resolves beatmap_id into a beatmapset response."""
    beatmapset = BeatmapSet(
        user_id=None,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Insane",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmap)
    await db_session.commit()

    response = await client.get(
        "/api/v2/beatmapsets/lookup",
        params={"beatmap_id": beatmap.id},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == beatmapset.id
    assert data["artist"] == beatmapset.artist
    assert data["title"] == beatmapset.title
    assert data["creator"] == beatmapset.creator
    assert len(data["beatmaps"]) == 1
    assert data["beatmaps"][0]["id"] == beatmap.id


@pytest.mark.asyncio
async def test_submit_score_response_contains_id_and_position(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Solo score submit response should include a positive id and position-compatible field."""

    async def fake_ensure_osu_file(self, beatmap: Beatmap) -> str | None:  # noqa: ARG001
        return None

    monkeypatch.setattr("app.api.v2.scores.BeatmapService.ensure_osu_file", fake_ensure_osu_file)

    user = User(
        username="scorecontract",
        email="scorecontract@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    for mode in GameMode:
        db_session.add(UserStatistics(user_id=user.id, mode=mode))

    beatmapset = BeatmapSet(
        user_id=None,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Insane",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
        checksum="abc123",
    )
    db_session.add(beatmap)
    await db_session.commit()

    token_pair = create_token_pair(user.id, ["*"])
    headers = {"Authorization": f"Bearer {token_pair.access_token}"}

    token_response = await client.post(
        f"/api/v2/beatmaps/{beatmap.id}/solo/scores",
        data={
            "version_hash": "build-test",
            "beatmap_hash": "abc123",
            "ruleset_id": 0,
        },
        headers=headers,
    )
    assert token_response.status_code == 200
    token_payload = token_response.json()
    assert token_payload["id"] > 0

    submit_response = await client.put(
        f"/api/v2/beatmaps/{beatmap.id}/solo/scores/{token_payload['id']}",
        headers=headers,
        json={
            "accuracy": 98.12,
            "max_combo": 543,
            "mods": [],
            "passed": True,
            "rank": "A",
            "statistics": {"great": 500, "ok": 20, "miss": 1},
            "maximum_statistics": {"great": 521},
            "total_score": 1234567,
            "total_score_without_mods": 1234567,
            "ruleset_id": 0,
            "pp": 111.11,
            "pauses": [],
        },
    )

    assert submit_response.status_code == 200
    payload = submit_response.json()
    assert payload["id"] > 0
    assert "position" in payload
    assert payload["position"] == payload["rank_global"]

    stored_score = (
        await db_session.execute(
            select(Score).where(Score.id == payload["id"]),
        )
    ).scalar_one_or_none()
    assert stored_score is not None


@pytest.mark.asyncio
async def test_get_beatmaps_requires_ids(client: AsyncClient) -> None:
    """Beatmaps list endpoint returns 400 when no ids are provided."""
    response = await client.get("/api/v2/beatmaps/")
    assert response.status_code == 400
    assert response.json()["detail"] == "Must provide at least one beatmap id via ids[]"


@pytest.mark.asyncio
async def test_builds_endpoint(client: AsyncClient) -> None:
    """Test builds endpoint required for client startup."""
    response = await client.get("/api/v2/changelog/builds")
    assert response.status_code == 200
    data = response.json()
    assert "builds" in data


@pytest.mark.asyncio
async def test_get_rankings_performance(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test performance rankings endpoint."""
    user1 = User(
        username="rank_user1",
        email="rank_user1@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    user2 = User(
        username="rank_user2",
        email="rank_user2@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add_all([user1, user2])
    await db_session.flush()

    db_session.add(UserStatistics(user_id=user1.id, mode=GameMode.OSU, pp=5000.0, ranked_score=1000000))
    db_session.add(UserStatistics(user_id=user2.id, mode=GameMode.OSU, pp=3000.0, ranked_score=2000000))
    await db_session.commit()

    token_pair = create_token_pair(user1.id, ["*"])
    response = await client.get(
        "/api/v2/rankings/osu/performance",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "performance"
    assert data["total"] == 2
    assert data["ranking"][0]["user"]["username"] == "rank_user1"
    assert data["ranking"][0]["rank"] == 1


@pytest.mark.asyncio
async def test_get_rankings_invalid_type(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test rankings endpoint with invalid ranking type."""
    user = User(
        username="rank_auth_user",
        email="rank_auth_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.commit()

    token_pair = create_token_pair(user.id, ["*"])
    response = await client.get(
        "/api/v2/rankings/osu/invalid",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_user_score_rank(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test user score rank aggregate endpoint."""
    user = User(
        username="score_user",
        email="score_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    competitor = User(
        username="score_competitor",
        email="score_competitor@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add_all([user, competitor])
    await db_session.flush()

    beatmapset = BeatmapSet(
        user_id=user.id,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=user.id,
        version="Hard",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmap)
    await db_session.flush()

    data_payload = json.dumps(
        {
            "mods": [],
            "statistics": {"great": 100, "ok": 10, "meh": 5, "miss": 1},
            "maximum_statistics": {"great": 116},
        },
    )

    higher_score = Score(
        user_id=competitor.id,
        beatmap_id=beatmap.id,
        ruleset_id=int(GameMode.OSU),
        data=data_payload,
        total_score=1500000,
        accuracy=99.1,
        pp=220.0,
        max_combo=1000,
        rank="S",
        passed=True,
        ranked=True,
    )
    user_score = Score(
        user_id=user.id,
        beatmap_id=beatmap.id,
        ruleset_id=int(GameMode.OSU),
        data=data_payload,
        total_score=1300000,
        accuracy=98.5,
        pp=200.0,
        max_combo=950,
        rank="A",
        passed=True,
        ranked=True,
    )
    db_session.add_all([higher_score, user_score])
    await db_session.commit()

    token_pair = create_token_pair(user.id, ["*"])
    response = await client.get(
        f"/api/v2/users/{user.id}/scores/rank",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["user"]["username"] == "score_user"
    assert data["position"] == 2
    assert data["score"]["id"] == user_score.id


@pytest.mark.asyncio
async def test_get_user_score_rank_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Test user score rank endpoint when user has no ranked score."""
    user = User(
        username="score_empty",
        email="score_empty@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.commit()

    token_pair = create_token_pair(user.id, ["*"])
    response = await client.get(
        f"/api/v2/users/{user.id}/scores/rank",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_score_replay_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Replay endpoint returns 404 when score replay does not exist."""
    user = User(
        username="replay_user_missing",
        email="replay_user_missing@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.commit()

    token_pair = create_token_pair(user.id, ["*"])
    response = await client.get(
        "/api/v2/scores/99999/replay",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_score_replay_success(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replay endpoint returns .osr file for authenticated users."""
    settings = get_settings()
    original_replays_path = settings.replays_path
    monkeypatch.setattr(settings, "replays_path", str(tmp_path))

    try:
        user = User(
            username="replay_user_success",
            email="replay_user_success@example.com",
            password_hash=get_password_hash("testpassword"),
            country_acronym="US",
        )
        db_session.add(user)
        await db_session.flush()

        beatmapset = BeatmapSet(
            user_id=user.id,
            artist="artist",
            title="title",
            creator="creator",
            status=BeatmapStatus.RANKED,
        )
        db_session.add(beatmapset)
        await db_session.flush()

        beatmap = Beatmap(
            beatmapset_id=beatmapset.id,
            user_id=user.id,
            version="Hard",
            mode=GameMode.OSU,
            status=BeatmapStatus.RANKED,
        )
        db_session.add(beatmap)
        await db_session.flush()

        score = Score(
            user_id=user.id,
            beatmap_id=beatmap.id,
            ruleset_id=int(GameMode.OSU),
            data=json.dumps({"mods": [], "statistics": {}, "maximum_statistics": {}}),
            total_score=123456,
            accuracy=98.5,
            pp=120.0,
            max_combo=500,
            rank="A",
            passed=True,
            ranked=True,
            has_replay=True,
        )
        db_session.add(score)
        await db_session.commit()

        replay_path = tmp_path / f"{score.id}.osr"
        replay_path.write_bytes(b"osr")

        token_pair = create_token_pair(user.id, ["*"])
        response = await client.get(
            f"/api/v2/scores/{score.id}/download",
            headers={"Authorization": f"Bearer {token_pair.access_token}"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/octet-stream")
        assert response.headers["content-disposition"].endswith(f'{score.id}.osr"')
        assert response.content
    finally:
        monkeypatch.setattr(settings, "replays_path", original_replays_path)


@pytest.mark.asyncio
async def test_pp_calculate_api_compatibility(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PP calculate endpoint keeps response shape stable across engine backends."""
    beatmapset = BeatmapSet(
        user_id=None,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=None,
        version="Normal",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmap)
    await db_session.commit()

    async def fake_ensure_osu_file(self: object, _beatmap: Beatmap) -> str:
        return "dummy.osu"

    def fake_calculate_pp(self: object, _osu_file_path: str, _params: object) -> dict[str, float | None]:
        return {
            "pp": 123.456,
            "stars": 6.78,
            "pp_aim": None,
            "pp_speed": None,
            "pp_acc": None,
            "pp_flashlight": None,
            "effective_miss_count": None,
            "pp_difficulty": None,
            "aim": 3.2,
            "speed": 2.9,
            "flashlight": 0.0,
        }

    monkeypatch.setattr("app.api.v2.pp.BeatmapService.ensure_osu_file", fake_ensure_osu_file)
    monkeypatch.setattr("app.api.v2.pp.PPService.calculate_pp", fake_calculate_pp)

    response = await client.get(
        "/api/v2/pp/calculate",
        params={
            "beatmap_id": beatmap.id,
            "mode": 0,
            "mods": 0,
            "acc": 98.5,
            "n300": 900,
            "n100": 30,
            "n50": 5,
            "nmiss": 2,
            "combo": 1200,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["pp"] == 123.456
    assert payload["stars"] == 6.78
    assert payload["details"]["pp_aim"] is None
    assert payload["details"]["pp_speed"] is None
    assert payload["details"]["pp_acc"] is None
    assert payload["details"]["pp_flashlight"] is None
