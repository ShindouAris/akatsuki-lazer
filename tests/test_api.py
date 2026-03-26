"""API endpoint tests."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_token_pair
from app.core.security import get_password_hash
from app.models.user import GameMode
from app.models.user import User
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
async def test_builds_endpoint(client: AsyncClient) -> None:
    """Test builds endpoint required for client startup."""
    response = await client.get("/api/v2/changelog/builds")
    assert response.status_code == 200
    data = response.json()
    assert "builds" in data
