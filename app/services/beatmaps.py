"""Beatmap service for fetching and caching beatmaps from mirror or official API."""

import logging
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus

logger = logging.getLogger(__name__)

# Official osu! API endpoints
OSU_API_BASE_URL = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"

# Public beatmap CDN (no auth required)
CATBOY_CDN_URL = "https://catboy.best"


@dataclass
class BeatmapsetSearchResult:
    """Result from beatmapset search."""

    beatmapsets: list[dict[str, Any]]
    cursor_string: str | None
    total: int


@dataclass
class OAuthToken:
    """OAuth2 token with expiry tracking."""

    access_token: str
    expires_at: float  # Unix timestamp


def _generate_cover_urls(beatmapset_id: int) -> dict[str, str]:
    """Generate cover URLs from osu! assets CDN."""
    base = f"https://assets.ppy.sh/beatmaps/{beatmapset_id}/covers"
    return {
        "cover": f"{base}/cover.jpg",
        "cover@2x": f"{base}/cover@2x.jpg",
        "card": f"{base}/card.jpg",
        "card@2x": f"{base}/card@2x.jpg",
        "list": f"{base}/list.jpg",
        "list@2x": f"{base}/list@2x.jpg",
        "slimcover": f"{base}/slimcover.jpg",
        "slimcover@2x": f"{base}/slimcover@2x.jpg",
    }


def _parse_status(status_str: str) -> BeatmapStatus:
    """Convert status string to BeatmapStatus enum."""
    status_map = {
        "graveyard": BeatmapStatus.GRAVEYARD,
        "wip": BeatmapStatus.WIP,
        "pending": BeatmapStatus.PENDING,
        "ranked": BeatmapStatus.RANKED,
        "approved": BeatmapStatus.APPROVED,
        "qualified": BeatmapStatus.QUALIFIED,
        "loved": BeatmapStatus.LOVED,
    }
    return status_map.get(status_str.lower(), BeatmapStatus.PENDING)


def _parse_mode(mode_str: str) -> int:
    """Convert mode string to mode int."""
    mode_map = {
        "osu": 0,
        "taiko": 1,
        "fruits": 2,
        "mania": 3,
    }
    return mode_map.get(mode_str.lower(), 0)


def _extract_beatmap_id_from_osu(osu_bytes: bytes) -> int | None:
    """Extract BeatmapID from .osu file contents."""
    try:
        content = osu_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = osu_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = osu_bytes.decode("latin-1", errors="ignore")

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("BeatmapID:"):
            continue
        _, value = stripped.split(":", 1)
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    return None


class BeatmapService:
    """Service for fetching beatmaps with mirror or official API support.

    Supports two modes controlled by USE_BEATMAP_MIRROR env var:
    - Mirror mode (default): Uses internal mirror (requires IP whitelist)
    - Direct mode: Uses official osu! API v2 (requires API credentials)
    """

    # Class-level token cache for official API
    _osu_token: OAuthToken | None = None

    def __init__(self, db: AsyncSession | None = None):
        self.db = db
        self._http_client: httpx.AsyncClient | None = None
        self._settings = get_settings()

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for mirror."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self._settings.beatmap_mirror_url,
                timeout=15.0,
            )
        return self._http_client

    async def _get_osu_api_client(self) -> httpx.AsyncClient:
        """Get HTTP client with OAuth2 token for official osu! API."""
        token = await self._get_osu_token()
        return httpx.AsyncClient(
            base_url=OSU_API_BASE_URL,
            timeout=15.0,
            headers={"Authorization": f"Bearer {token}"},
        )

    def _get_catboy_client(self) -> httpx.AsyncClient:
        """Get HTTP client for catboy.best CDN (no auth required)."""
        return httpx.AsyncClient(
            base_url=CATBOY_CDN_URL,
            timeout=120.0,
            follow_redirects=True,
        )

    async def _get_osu_token(self) -> str:
        """Get valid OAuth2 token for official osu! API, refreshing if needed."""
        # Check if we have a valid cached token
        if (
            BeatmapService._osu_token
            and BeatmapService._osu_token.expires_at > time.time() + 60
        ):
            return BeatmapService._osu_token.access_token

        # Request new token using client credentials
        logger.info("Requesting new osu! API token")
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                OSU_TOKEN_URL,
                data={
                    "client_id": self._settings.osu_api_client_id,
                    "client_secret": self._settings.osu_api_client_secret,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
            )
            response.raise_for_status()
            data = response.json()

            BeatmapService._osu_token = OAuthToken(
                access_token=data["access_token"],
                expires_at=time.time() + data["expires_in"],
            )
            return BeatmapService._osu_token.access_token

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def ensure_osu_file(self, beatmap: Beatmap) -> str | None:
        """Ensure a beatmap .osu file is cached locally and return its path."""
        if beatmap.beatmapset_id is None:
            return None

        root_dir = Path(self._settings.beatmaps_path)
        osu_dir = root_dir / "osu"
        osz_dir = root_dir / "osz"
        osu_dir.mkdir(parents=True, exist_ok=True)
        osz_dir.mkdir(parents=True, exist_ok=True)

        osu_file_path = osu_dir / f"{beatmap.id}.osu"
        if osu_file_path.exists():
            return str(osu_file_path)

        osz_file_path = osz_dir / f"{beatmap.beatmapset_id}.osz"
        if not osz_file_path.exists():
            try:
                with osz_file_path.open("wb") as osz_file:
                    async for chunk in self.download_beatmapset(beatmap.beatmapset_id):
                        osz_file.write(chunk)
            except Exception as exc:
                logger.error(
                    "Failed to download beatmapset %s for .osu extraction: %s",
                    beatmap.beatmapset_id,
                    exc,
                )
                return None

        try:
            with open(osz_file_path, "rb") as archive:
                for archive_name in archive.namelist():
                    if not archive_name.lower().endswith(".osu"):
                        continue

                    osu_bytes = archive.read(archive_name)
                    if _extract_beatmap_id_from_osu(osu_bytes) != beatmap.id:
                        continue

                    osu_file_path.write_bytes(osu_bytes)
                    return str(osu_file_path)
        except Exception as exc:
            logger.error("Failed to extract .osu file for beatmap %s: %s", beatmap.id, exc)
            return None

        logger.warning(
            "Could not find beatmap id %s in beatmapset archive %s",
            beatmap.id,
            beatmap.beatmapset_id,
        )
        return None

    # ==================== Download Methods ====================

    async def download_beatmapset(
        self, beatmapset_id: int, no_video: bool = False,
    ) -> AsyncIterator[bytes]:
        """Download a beatmapset as .osz file.

        Uses catboy.best CDN by default (publicly available, no auth required).
        """
        # Always use catboy.best CDN for downloads - it's public and reliable
        async for chunk in self._download_from_catboy(beatmapset_id, no_video):
            yield chunk

    async def _download_from_catboy(
        self, beatmapset_id: int, no_video: bool = False,
    ) -> AsyncIterator[bytes]:
        """Download beatmapset from catboy.best CDN."""
        path = f"/d/{beatmapset_id}"
        if no_video:
            path += "n"  # 'n' suffix for no-video version

        async with self._get_catboy_client() as client:
            async with client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise Exception(
                        f"Failed to download beatmapset {beatmapset_id}: {response.status_code}",
                    )
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    async def _download_from_osu_api(
        self, beatmapset_id: int, no_video: bool = False,
    ) -> AsyncIterator[bytes]:
        """Download beatmapset from official osu! API (requires OAuth2).

        Currently unused - kept for future use if authenticated downloads are needed.
        """
        path = f"/beatmapsets/{beatmapset_id}/download"
        if no_video:
            path += "?noVideo=1"

        async with await self._get_osu_api_client() as client:
            async with client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise Exception(
                        f"Failed to download beatmapset {beatmapset_id}: {response.status_code}",
                    )
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    async def get_beatmap(self, beatmap_id: int) -> Beatmap | None:
        """Get a beatmap by ID, fetching from external source if not in database."""
        # 1. Check local database first
        result = await self.db.execute(
            select(Beatmap)
            .options(selectinload(Beatmap.beatmapset))
            .where(Beatmap.id == beatmap_id),
        )
        beatmap = result.scalar_one_or_none()

        if beatmap:
            return beatmap

        # 2. Fetch from external source
        logger.info(f"Beatmap {beatmap_id} not in database, fetching externally")
        if self._settings.use_beatmap_mirror:
            beatmap_data = await self._fetch_beatmap_from_mirror(beatmap_id)
        else:
            beatmap_data = await self._fetch_beatmap_from_osu_api(beatmap_id)

        if not beatmap_data:
            return None

        # 3. Cache in database and return
        beatmap = await self._cache_beatmap(beatmap_data)
        return beatmap

    async def get_beatmap_by_checksum(
        self, checksum: str, filename: str | None = None,
    ) -> Beatmap | None:
        """Get a beatmap by checksum, fetching from osu! API if not in database.

        Note: Mirror doesn't support checksum lookup, only the official osu! API does.
        """
        # 1. Check local database first
        result = await self.db.execute(
            select(Beatmap)
            .options(selectinload(Beatmap.beatmapset))
            .where(Beatmap.checksum == checksum),
        )
        beatmap = result.scalar_one_or_none()

        if beatmap:
            return beatmap

        # 2. Try osu! API lookup (only works when not using mirror)
        if not self._settings.use_beatmap_mirror:
            logger.info(f"Beatmap with checksum {checksum[:8]}... not in database, looking up via osu! API")
            beatmap_data = await self._lookup_beatmap_from_osu_api(
                checksum=checksum, filename=filename,
            )
            if beatmap_data:
                beatmap = await self._cache_beatmap(beatmap_data)
                return beatmap

        return None

    async def get_beatmapset(self, beatmapset_id: int) -> BeatmapSet | None:
        """Get a beatmapset by ID, fetching from external source if not in database."""
        # 1. Check local database first
        result = await self.db.execute(
            select(BeatmapSet)
            .options(selectinload(BeatmapSet.beatmaps))
            .where(BeatmapSet.id == beatmapset_id),
        )
        beatmapset = result.scalar_one_or_none()

        if beatmapset:
            return beatmapset

        # 2. Fetch from external source
        logger.info(f"Beatmapset {beatmapset_id} not in database, fetching externally")
        if self._settings.use_beatmap_mirror:
            beatmapset_data = await self._fetch_beatmapset_from_mirror(beatmapset_id)
        else:
            beatmapset_data = await self._fetch_beatmapset_from_osu_api(beatmapset_id)

        if not beatmapset_data:
            return None

        # 3. Cache in database and return
        beatmapset = await self._cache_beatmapset(beatmapset_data)
        return beatmapset

    # ==================== Mirror API Methods ====================

    async def _fetch_beatmap_from_mirror(self, beatmap_id: int) -> dict[str, Any] | None:
        """Fetch beatmap data from mirror service."""
        try:
            client = await self._get_http_client()
            response = await client.get(f"/api/osu-api/v2/beatmaps/{beatmap_id}")

            if response.status_code == 404:
                logger.debug(f"Beatmap {beatmap_id} not found on mirror")
                return None

            if response.status_code != 200:
                logger.warning(
                    f"Mirror returned {response.status_code} for beatmap {beatmap_id}",
                )
                return None

            return response.json()

        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching beatmap {beatmap_id} from mirror")
            return None
        except Exception as e:
            logger.error(f"Error fetching beatmap {beatmap_id} from mirror: {e}")
            return None

    async def _fetch_beatmapset_from_mirror(
        self, beatmapset_id: int,
    ) -> dict[str, Any] | None:
        """Fetch beatmapset data from mirror service."""
        try:
            client = await self._get_http_client()
            response = await client.get(f"/api/osu-api/v2/beatmapsets/{beatmapset_id}")

            if response.status_code == 404:
                logger.debug(f"Beatmapset {beatmapset_id} not found on mirror")
                return None

            if response.status_code != 200:
                logger.warning(
                    f"Mirror returned {response.status_code} for beatmapset {beatmapset_id}",
                )
                return None

            return response.json()

        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching beatmapset {beatmapset_id} from mirror")
            return None
        except Exception as e:
            logger.error(f"Error fetching beatmapset {beatmapset_id} from mirror: {e}")
            return None

    # ==================== Official osu! API Methods ====================

    async def _lookup_beatmap_from_osu_api(
        self,
        checksum: str | None = None,
        filename: str | None = None,
        beatmap_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Lookup beatmap from official osu! API v2 using checksum, filename, or ID."""
        try:
            async with await self._get_osu_api_client() as client:
                params: dict[str, Any] = {}
                if beatmap_id and beatmap_id > 0:
                    params["id"] = beatmap_id
                if checksum:
                    params["checksum"] = checksum
                if filename:
                    params["filename"] = filename

                if not params:
                    return None

                response = await client.get("/beatmaps/lookup", params=params)

                if response.status_code == 404:
                    logger.debug(f"Beatmap lookup returned 404 for params {params}")
                    return None

                if response.status_code == 401:
                    # Token might be expired, clear cache and retry once
                    BeatmapService._osu_token = None
                    async with await self._get_osu_api_client() as retry_client:
                        response = await retry_client.get("/beatmaps/lookup", params=params)

                if response.status_code != 200:
                    logger.warning(
                        f"osu! API lookup returned {response.status_code} for params {params}",
                    )
                    return None

                return response.json()

        except httpx.TimeoutException:
            logger.warning("Timeout looking up beatmap from osu! API")
            return None
        except Exception as e:
            logger.error(f"Error looking up beatmap from osu! API: {e}")
            return None

    async def _fetch_beatmap_from_osu_api(
        self, beatmap_id: int,
    ) -> dict[str, Any] | None:
        """Fetch beatmap data from official osu! API v2."""
        try:
            async with await self._get_osu_api_client() as client:
                response = await client.get(f"/beatmaps/{beatmap_id}")

                if response.status_code == 404:
                    logger.debug(f"Beatmap {beatmap_id} not found on osu! API")
                    return None

                if response.status_code == 401:
                    # Token might be expired, clear cache and retry once
                    BeatmapService._osu_token = None
                    async with await self._get_osu_api_client() as retry_client:
                        response = await retry_client.get(f"/beatmaps/{beatmap_id}")

                if response.status_code != 200:
                    logger.warning(
                        f"osu! API returned {response.status_code} for beatmap {beatmap_id}",
                    )
                    return None

                return response.json()

        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching beatmap {beatmap_id} from osu! API")
            return None
        except Exception as e:
            logger.error(f"Error fetching beatmap {beatmap_id} from osu! API: {e}")
            return None

    async def _fetch_beatmapset_from_osu_api(
        self, beatmapset_id: int,
    ) -> dict[str, Any] | None:
        """Fetch beatmapset data from official osu! API v2."""
        try:
            async with await self._get_osu_api_client() as client:
                response = await client.get(f"/beatmapsets/{beatmapset_id}")

                if response.status_code == 404:
                    logger.debug(f"Beatmapset {beatmapset_id} not found on osu! API")
                    return None

                if response.status_code == 401:
                    # Token might be expired, clear cache and retry once
                    BeatmapService._osu_token = None
                    async with await self._get_osu_api_client() as retry_client:
                        response = await retry_client.get(f"/beatmapsets/{beatmapset_id}")

                if response.status_code != 200:
                    logger.warning(
                        f"osu! API returned {response.status_code} for beatmapset {beatmapset_id}",
                    )
                    return None

                return response.json()

        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching beatmapset {beatmapset_id} from osu! API")
            return None
        except Exception as e:
            logger.error(f"Error fetching beatmapset {beatmapset_id} from osu! API: {e}")
            return None

    async def _search_beatmapsets_osu_api(
        self,
        query: str | None = None,
        mode: int | None = None,
        status: str | None = None,
        cursor_string: str | None = None,
    ) -> BeatmapsetSearchResult:
        """Search beatmapsets using official osu! API v2."""
        try:
            async with await self._get_osu_api_client() as client:
                params: dict[str, Any] = {}
                if query:
                    params["q"] = query
                if mode is not None:
                    params["m"] = mode
                if status:
                    # Map our status to osu! API status parameter
                    status_map = {
                        "ranked": "ranked",
                        "approved": "ranked",  # osu! API groups these
                        "qualified": "qualified",
                        "loved": "loved",
                        "pending": "pending",
                        "graveyard": "graveyard",
                        "wip": "pending",
                    }
                    params["s"] = status_map.get(status, "any")
                if cursor_string:
                    params["cursor_string"] = cursor_string

                response = await client.get("/beatmapsets/search", params=params)

                if response.status_code == 401:
                    BeatmapService._osu_token = None
                    async with await self._get_osu_api_client() as retry_client:
                        response = await retry_client.get(
                            "/beatmapsets/search", params=params,
                        )

                if response.status_code != 200:
                    logger.warning(f"osu! API search returned {response.status_code}")
                    return BeatmapsetSearchResult(
                        beatmapsets=[], cursor_string=None, total=0,
                    )

                data = response.json()

                # Convert osu! API format to our format
                beatmapsets = []
                for bs in data.get("beatmapsets", []):
                    beatmapsets.append(self._osu_api_to_v2_beatmapset(bs))

                return BeatmapsetSearchResult(
                    beatmapsets=beatmapsets,
                    cursor_string=data.get("cursor_string"),
                    total=data.get("total", len(beatmapsets)),
                )

        except httpx.TimeoutException:
            logger.warning("Timeout searching beatmapsets from osu! API")
            return BeatmapsetSearchResult(beatmapsets=[], cursor_string=None, total=0)
        except Exception as e:
            logger.error(f"Error searching beatmapsets from osu! API: {e}")
            return BeatmapsetSearchResult(beatmapsets=[], cursor_string=None, total=0)

    def _osu_api_to_v2_beatmapset(self, bs: dict[str, Any]) -> dict[str, Any]:
        """Convert official osu! API beatmapset to our response format."""
        beatmaps = []
        for bm in bs.get("beatmaps", []):
            beatmaps.append({
                "id": bm.get("id"),
                "beatmapset_id": bs.get("id"),
                "version": bm.get("version", ""),
                "mode": bm.get("mode", "osu"),
                "status": bs.get("status", "pending"),
                "difficulty_rating": bm.get("difficulty_rating", 0.0),
                "total_length": bm.get("total_length", 0),
                "cs": bm.get("cs", 5.0),
                "ar": bm.get("ar", 5.0),
                "od": bm.get("accuracy", 5.0),
                "hp": bm.get("drain", 5.0),
                "bpm": bm.get("bpm", 0.0),
                "max_combo": bm.get("max_combo"),
                "checksum": bm.get("checksum"),
            })

        # Pass through covers from osu! API response
        covers = bs.get("covers", {})

        return {
            "id": bs.get("id"),
            "artist": bs.get("artist", ""),
            "artist_unicode": bs.get("artist_unicode"),
            "title": bs.get("title", ""),
            "title_unicode": bs.get("title_unicode"),
            "creator": bs.get("creator", ""),
            "user_id": bs.get("user_id"),
            "status": bs.get("status", "pending"),
            "play_count": bs.get("play_count", 0),
            "favourite_count": bs.get("favourite_count", 0),
            "source": bs.get("source", ""),
            "tags": bs.get("tags", ""),
            "ranked_date": bs.get("ranked_date"),
            "submitted_date": bs.get("submitted_date"),
            "last_updated": bs.get("last_updated"),
            "bpm": bs.get("bpm", 0.0),
            "preview_url": bs.get("preview_url"),
            "video": bs.get("video", False),
            "storyboard": bs.get("storyboard", False),
            "nsfw": bs.get("nsfw", False),
            "covers": covers,
            "beatmaps": beatmaps,
        }

    # ==================== Search (uses mirror cheesegull API) ====================

    async def search_beatmapsets(
        self,
        query: str | None = None,
        mode: int | None = None,
        status: str | None = None,
        sort: str = "relevance_desc",
        cursor_string: str | None = None,
    ) -> BeatmapsetSearchResult:
        """Search beatmapsets.

        Uses cheesegull API on mirror, or official osu! API v2 based on config.
        """
        if not self._settings.use_beatmap_mirror:
            return await self._search_beatmapsets_osu_api(
                query=query, mode=mode, status=status, cursor_string=cursor_string,
            )

        # Use mirror's cheesegull search API
        try:
            client = await self._get_http_client()

            # Convert status to cheesegull format
            cheesegull_status = None
            if status:
                status_map = {
                    "ranked": 1,
                    "approved": 2,
                    "qualified": 3,
                    "loved": 4,
                    "pending": 0,
                    "graveyard": 0,
                    "wip": 0,
                }
                cheesegull_status = status_map.get(status)

            # Parse cursor for offset
            offset = 0
            if cursor_string:
                try:
                    offset = int(cursor_string)
                except ValueError:
                    pass

            params: dict[str, Any] = {
                "query": query or "",
                "offset": offset,
                "amount": 50,
            }
            if cheesegull_status is not None:
                params["status"] = cheesegull_status
            if mode is not None:
                params["mode"] = mode

            response = await client.get("/api/search", params=params)

            if response.status_code != 200:
                logger.warning(f"Mirror search returned {response.status_code}")
                return BeatmapsetSearchResult(
                    beatmapsets=[], cursor_string=None, total=0,
                )

            cheesegull_results = response.json()

            # Convert cheesegull format to osu! API v2 format
            beatmapsets = []
            for cg_set in cheesegull_results:
                beatmapset = self._cheesegull_to_v2_beatmapset(cg_set)
                beatmapsets.append(beatmapset)

            # Calculate next cursor
            next_cursor = None
            if len(beatmapsets) == 50:
                next_cursor = str(offset + 50)

            return BeatmapsetSearchResult(
                beatmapsets=beatmapsets,
                cursor_string=next_cursor,
                total=len(beatmapsets),
            )

        except httpx.TimeoutException:
            logger.warning("Timeout searching beatmapsets from mirror")
            return BeatmapsetSearchResult(beatmapsets=[], cursor_string=None, total=0)
        except Exception as e:
            logger.error(f"Error searching beatmapsets from mirror: {e}")
            return BeatmapsetSearchResult(beatmapsets=[], cursor_string=None, total=0)

    def _cheesegull_to_v2_beatmapset(self, cg_set: dict[str, Any]) -> dict[str, Any]:
        """Convert cheesegull beatmapset format to osu! API v2 format."""
        status_map = {
            -2: "graveyard",
            -1: "wip",
            0: "pending",
            1: "ranked",
            2: "approved",
            3: "qualified",
            4: "loved",
        }
        status = status_map.get(cg_set.get("RankedStatus", 0), "pending")

        beatmaps = []
        for cg_bm in cg_set.get("ChildrenBeatmaps", []):
            mode_map = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
            beatmaps.append({
                "id": cg_bm.get("BeatmapID"),
                "beatmapset_id": cg_set.get("SetID"),
                "version": cg_bm.get("DiffName", ""),
                "mode": mode_map.get(cg_bm.get("Mode", 0), "osu"),
                "status": status,
                "difficulty_rating": cg_bm.get("DifficultyRating", 0.0),
                "total_length": cg_bm.get("TotalLength", 0),
                "cs": cg_bm.get("CS", 5.0),
                "ar": cg_bm.get("AR", 5.0),
                "od": cg_bm.get("OD", 5.0),
                "hp": cg_bm.get("HP", 5.0),
                "bpm": cg_bm.get("BPM", 0.0),
                "max_combo": cg_bm.get("MaxCombo"),
                "checksum": cg_bm.get("FileMD5"),
            })

        # Generate cover URLs from osu! assets CDN
        set_id = cg_set.get("SetID")
        covers = _generate_cover_urls(set_id)

        return {
            "id": set_id,
            "artist": cg_set.get("Artist", ""),
            "artist_unicode": cg_set.get("Artist"),
            "title": cg_set.get("Title", ""),
            "title_unicode": cg_set.get("Title"),
            "creator": cg_set.get("Creator", ""),
            "user_id": None,
            "status": status,
            "play_count": 0,
            "favourite_count": cg_set.get("Favourites", 0),
            "source": cg_set.get("Source", ""),
            "tags": cg_set.get("Tags", ""),
            "ranked_date": cg_set.get("ApprovedDate"),
            "submitted_date": None,
            "last_updated": cg_set.get("LastUpdate"),
            "bpm": beatmaps[0].get("bpm", 0.0) if beatmaps else 0.0,
            "preview_url": None,
            "video": cg_set.get("HasVideo", False),
            "storyboard": False,
            "nsfw": False,
            "covers": covers,
            "beatmaps": beatmaps,
        }

    # ==================== Caching Methods ====================

    async def _cache_beatmap(self, data: dict[str, Any]) -> Beatmap:
        """Cache beatmap data in database."""
        # Check if beatmap already exists
        result = await self.db.execute(
            select(Beatmap)
            .options(selectinload(Beatmap.beatmapset))
            .where(Beatmap.id == data["id"]),
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        beatmapset_id = data.get("beatmapset_id")
        if beatmapset_id:
            result = await self.db.execute(
                select(BeatmapSet).where(BeatmapSet.id == beatmapset_id),
            )
            if not result.scalar_one_or_none():
                # Fetch and cache the beatmapset too
                if self._settings.use_beatmap_mirror:
                    beatmapset_data = await self._fetch_beatmapset_from_mirror(
                        beatmapset_id,
                    )
                else:
                    beatmapset_data = await self._fetch_beatmapset_from_osu_api(
                        beatmapset_id,
                    )
                if beatmapset_data:
                    await self._cache_beatmapset(beatmapset_data)
                    # Re-check if beatmap was cached as part of beatmapset
                    result = await self.db.execute(
                        select(Beatmap)
                        .options(selectinload(Beatmap.beatmapset))
                        .where(Beatmap.id == data["id"]),
                    )
                    existing = result.scalar_one_or_none()
                    if existing:
                        return existing

        beatmap = Beatmap(
            id=data["id"],
            beatmapset_id=beatmapset_id,
            mode=_parse_mode(data.get("mode", "osu")),
            version=data.get("version", ""),
            status=_parse_status(data.get("status", "pending")),
            checksum=data.get("checksum"),
            difficulty_rating=data.get("difficulty_rating", 0.0),
            total_length=data.get("total_length", 0),
            hit_length=data.get("hit_length", 0),
            bpm=data.get("bpm", 0.0),
            cs=data.get("cs", 5.0),
            ar=data.get("ar", 5.0),
            od=data.get("accuracy", 5.0),
            hp=data.get("drain", 5.0),
            max_combo=data.get("max_combo"),
            count_circles=data.get("count_circles", 0),
            count_sliders=data.get("count_sliders", 0),
            count_spinners=data.get("count_spinners", 0),
            play_count=data.get("playcount", 0),
            pass_count=data.get("passcount", 0),
        )

        self.db.add(beatmap)
        await self.db.commit()
        await self.db.refresh(beatmap)

        logger.info(f"Cached beatmap {beatmap.id} in database")
        return beatmap

    async def _cache_beatmapset(self, data: dict[str, Any]) -> BeatmapSet:
        """Cache beatmapset data in database."""
        result = await self.db.execute(
            select(BeatmapSet).where(BeatmapSet.id == data["id"]),
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        beatmapset = BeatmapSet(
            id=data["id"],
            user_id=data.get("user_id"),
            artist=data.get("artist", ""),
            artist_unicode=data.get("artist_unicode"),
            title=data.get("title", ""),
            title_unicode=data.get("title_unicode"),
            creator=data.get("creator", ""),
            source=data.get("source"),
            tags=data.get("tags"),
            status=_parse_status(data.get("status", "pending")),
            bpm=data.get("bpm", 0.0),
            play_count=data.get("play_count", 0),
            favourite_count=data.get("favourite_count", 0),
            has_video=data.get("video", False),
            has_storyboard=data.get("storyboard", False),
            nsfw=data.get("nsfw", False),
        )

        self.db.add(beatmapset)

        beatmaps_data = data.get("beatmaps", [])
        for bm_data in beatmaps_data:
            result = await self.db.execute(
                select(Beatmap).where(Beatmap.id == bm_data["id"]),
            )
            if result.scalar_one_or_none():
                continue

            beatmap = Beatmap(
                id=bm_data["id"],
                beatmapset_id=data["id"],
                mode=_parse_mode(bm_data.get("mode", "osu")),
                version=bm_data.get("version", ""),
                status=_parse_status(bm_data.get("status", "pending")),
                checksum=bm_data.get("checksum"),
                difficulty_rating=bm_data.get("difficulty_rating", 0.0),
                total_length=bm_data.get("total_length", 0),
                hit_length=bm_data.get("hit_length", 0),
                bpm=bm_data.get("bpm", 0.0),
                cs=bm_data.get("cs", 5.0),
                ar=bm_data.get("ar", 5.0),
                od=bm_data.get("accuracy", 5.0),
                hp=bm_data.get("drain", 5.0),
                max_combo=bm_data.get("max_combo"),
                count_circles=bm_data.get("count_circles", 0),
                count_sliders=bm_data.get("count_sliders", 0),
                count_spinners=bm_data.get("count_spinners", 0),
                play_count=bm_data.get("playcount", 0),
                pass_count=bm_data.get("passcount", 0),
            )
            self.db.add(beatmap)

        await self.db.commit()
        await self.db.refresh(beatmapset)

        logger.info(
            f"Cached beatmapset {beatmapset.id} with {len(beatmaps_data)} beatmaps",
        )
        return beatmapset
