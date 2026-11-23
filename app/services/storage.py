"""Object storage for replays and beatmap files.

Files live in an S3-compatible bucket. Backends form a chain: the primary S3 bucket first, then SeaweedFS (its S3
gateway), and finally the local disk, which is also what is used when nothing else is configured (development,
tests). A write goes to the first backend that accepts it; a read asks the backends in order until one has the
key. A backend that is down is skipped (and logged), a key that simply is not there is not an error.

Keys:  ``replays/<score id>.osr``   ``beatmaps/osz/<set id>.osz``   ``beatmaps/osu/<beatmap id>.osu``
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class StorageError(Exception):
    """No backend could complete the operation."""


class StorageBackend(Protocol):
    """One place files can be kept. Methods raise on a failure and report a missing key as ``None``/``False``."""

    name: str

    async def put_bytes(self, key: str, data: bytes) -> None: ...

    async def put_file(self, key: str, path: Path) -> None: ...

    async def get_bytes(self, key: str) -> bytes | None: ...

    async def download_to(self, key: str, path: Path) -> bool: ...

    async def exists(self, key: str) -> bool: ...


class LocalBackend:
    """Files on the local disk, laid out as before: replays under ``replays_path``, beatmaps under ``beatmaps_path``."""

    name = "local"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def path_for(self, key: str) -> Path:
        group, _, rest = key.partition("/")
        if group == "replays":
            return Path(self._settings.replays_path) / rest
        if group == "beatmaps":
            return Path(self._settings.beatmaps_path) / rest
        return Path(self._settings.beatmaps_path).parent / key

    async def put_bytes(self, key: str, data: bytes) -> None:
        def write() -> None:
            path = self.path_for(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)

    async def put_file(self, key: str, path: Path) -> None:
        def copy() -> None:
            target = self.path_for(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve() != path.resolve():
                shutil.copyfile(path, target)

        await asyncio.to_thread(copy)

    async def get_bytes(self, key: str) -> bytes | None:
        path = self.path_for(key)
        return await asyncio.to_thread(lambda: path.read_bytes() if path.is_file() else None)

    async def download_to(self, key: str, path: Path) -> bool:
        def copy() -> bool:
            source = self.path_for(key)
            if not source.is_file():
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() != path.resolve():
                shutil.copyfile(source, path)
            return True

        return await asyncio.to_thread(copy)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.path_for(key).is_file)


class S3Backend:
    """A bucket on any S3-compatible service (AWS S3, SeaweedFS' S3 gateway, ...)."""

    def __init__(
        self,
        *,
        name: str,
        bucket: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        timeout: float,
        client: object | None = None,
    ) -> None:
        self.name = name
        self._bucket = bucket
        self._endpoint_url = endpoint_url or None
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        self._region = region
        self._timeout = timeout
        self._client = client
        self._bucket_ready = False

    def _s3(self):  # noqa: ANN202 - boto3 clients have no useful static type
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=Config(
                    connect_timeout=self._timeout,
                    read_timeout=self._timeout * 3,
                    retries={"max_attempts": 1, "mode": "standard"},  # no retry: the next backend is the retry
                    s3={"addressing_style": "path"},  # SeaweedFS and most self-hosted gateways need path style
                ),
            )
        return self._client

    @staticmethod
    def _is_missing(error: Exception) -> bool:
        from botocore.exceptions import ClientError

        if not isinstance(error, ClientError):
            return False
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = str(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        return code in _NOT_FOUND_CODES or status == "404"

    def _ensure_bucket(self) -> None:
        """Create the bucket on first use; a missing bucket is an operator mistake only on AWS, here it is a setup step."""
        if self._bucket_ready:
            return
        s3 = self._s3()
        try:
            s3.head_bucket(Bucket=self._bucket)
        except Exception as error:  # noqa: BLE001
            if not self._is_missing(error):
                raise
            s3.create_bucket(Bucket=self._bucket)
            logger.info("Created storage bucket %s on %s", self._bucket, self.name)
        self._bucket_ready = True

    async def put_bytes(self, key: str, data: bytes) -> None:
        def put() -> None:
            self._ensure_bucket()
            self._s3().put_object(Bucket=self._bucket, Key=key, Body=data)

        await asyncio.to_thread(put)

    async def put_file(self, key: str, path: Path) -> None:
        def put() -> None:
            self._ensure_bucket()
            self._s3().upload_file(str(path), self._bucket, key)

        await asyncio.to_thread(put)

    async def get_bytes(self, key: str) -> bytes | None:
        def get() -> bytes | None:
            try:
                return self._s3().get_object(Bucket=self._bucket, Key=key)["Body"].read()
            except Exception as error:  # noqa: BLE001
                if self._is_missing(error):
                    return None
                raise

        return await asyncio.to_thread(get)

    async def download_to(self, key: str, path: Path) -> bool:
        def download() -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".part")
            try:
                self._s3().download_file(self._bucket, key, str(partial))
            except Exception as error:  # noqa: BLE001
                partial.unlink(missing_ok=True)
                if self._is_missing(error):
                    return False
                raise
            partial.replace(path)
            return True

        return await asyncio.to_thread(download)

    async def exists(self, key: str) -> bool:
        def head() -> bool:
            try:
                self._s3().head_object(Bucket=self._bucket, Key=key)
            except Exception as error:  # noqa: BLE001
                if self._is_missing(error):
                    return False
                raise
            return True

        return await asyncio.to_thread(head)


class ObjectStorage:
    """The chain of backends. See the module docstring."""

    def __init__(
        self,
        backends: list[StorageBackend],
        *,
        cooldown_seconds: float = 30.0,
        quick_timeout: float | None = None,
        file_timeout: float | None = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """quick_timeout bounds one small operation on one backend, file_timeout a whole-file transfer. A backend that
        takes longer counts as failed (DNS or connect hangs are not always covered by the client's own timeouts)."""
        if not backends:
            raise ValueError("at least one storage backend is needed")
        self.backends = backends
        self._quick = quick_timeout
        self._file = file_timeout
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._down_until: dict[str, float] = {}

    def _candidates(self) -> list[StorageBackend]:
        """The backends worth trying: one that just failed is skipped for a while, so a dead server does not add its
        timeout to every request. The last backend is always tried, and if every one is cooling down, all are."""
        now = self._clock()
        ready = [b for b in self.backends if self._down_until.get(b.name, 0.0) <= now]
        return ready or list(self.backends)

    def _failed(self, backend: StorageBackend) -> None:
        if backend is not self.backends[-1]:
            self._down_until[backend.name] = self._clock() + self._cooldown

    def _worked(self, backend: StorageBackend) -> None:
        self._down_until.pop(backend.name, None)

    async def put_bytes(self, key: str, data: bytes) -> str:
        """Store ``data``; returns the name of the backend that took it."""
        return await self._write(lambda backend: backend.put_bytes(key, data), key, self._quick)

    async def put_file(self, key: str, path: Path) -> str:
        """Store a local file; returns the name of the backend that took it."""
        return await self._write(lambda backend: backend.put_file(key, path), key, self._file)

    async def get_bytes(self, key: str) -> bytes | None:
        for backend in self._candidates():
            try:
                data = await asyncio.wait_for(backend.get_bytes(key), self._quick)
            except Exception as error:  # noqa: BLE001
                logger.warning("Storage backend %s failed reading %s: %s", backend.name, key, error)
                self._failed(backend)
                continue
            self._worked(backend)
            if data is not None:
                return data
        return None

    async def download_to(self, key: str, path: Path) -> bool:
        """Copy a stored file to ``path``; False when no backend has it."""
        for backend in self._candidates():
            try:
                found = await asyncio.wait_for(backend.download_to(key, path), self._file)
            except Exception as error:  # noqa: BLE001
                logger.warning("Storage backend %s failed downloading %s: %s", backend.name, key, error)
                self._failed(backend)
                continue
            self._worked(backend)
            if found:
                return True
        return False

    async def exists(self, key: str) -> bool:
        for backend in self._candidates():
            try:
                found = await asyncio.wait_for(backend.exists(key), self._quick)
            except Exception as error:  # noqa: BLE001
                logger.warning("Storage backend %s failed checking %s: %s", backend.name, key, error)
                self._failed(backend)
                continue
            self._worked(backend)
            if found:
                return True
        return False

    async def _write(self, action, key: str, timeout: float | None) -> str:  # noqa: ANN001
        failures: list[str] = []
        for backend in self._candidates():
            try:
                await asyncio.wait_for(action(backend), timeout)
            except Exception as error:  # noqa: BLE001
                logger.warning("Storage backend %s failed writing %s: %s", backend.name, key, error)
                failures.append(f"{backend.name}: {type(error).__name__}")
                self._failed(backend)
                continue
            self._worked(backend)
            if failures:
                logger.warning("Stored %s on %s after %s", key, backend.name, "; ".join(failures))
            return backend.name
        raise StorageError(f"could not store {key}: {'; '.join(failures)}")


def build_storage(settings: Settings) -> ObjectStorage:
    """The backends the settings ask for: S3, then SeaweedFS, then the local disk."""
    backends: list[StorageBackend] = []

    if settings.s3_bucket:
        backends.append(
            S3Backend(
                name="s3",
                bucket=settings.s3_bucket,
                endpoint_url=settings.s3_endpoint_url,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                region=settings.s3_region,
                timeout=settings.storage_timeout_seconds,
            ),
        )
    if settings.seaweedfs_endpoint_url:
        backends.append(
            S3Backend(
                name="seaweedfs",
                bucket=settings.seaweedfs_bucket,
                endpoint_url=settings.seaweedfs_endpoint_url,
                access_key=settings.seaweedfs_access_key,
                secret_key=settings.seaweedfs_secret_key,
                region=settings.s3_region,
                timeout=settings.storage_timeout_seconds,
            ),
        )

    backends.append(LocalBackend(settings))
    return ObjectStorage(backends, quick_timeout=settings.storage_timeout_seconds + 1)


_cache: dict[tuple, ObjectStorage] = {}


def get_storage() -> ObjectStorage:
    """The storage chain for the current settings (clients are reused between calls)."""
    settings = get_settings()
    signature = (
        settings.s3_bucket, settings.s3_endpoint_url, settings.s3_access_key, settings.s3_region,
        settings.seaweedfs_endpoint_url, settings.seaweedfs_bucket, settings.seaweedfs_access_key,
        settings.storage_timeout_seconds, settings.replays_path, settings.beatmaps_path,
    )
    storage = _cache.get(signature)
    if storage is None:
        storage = _cache[signature] = build_storage(settings)
    return storage


def replay_key(score_id: int) -> str:
    return f"replays/{score_id}.osr"


def osz_key(beatmapset_id: int) -> str:
    return f"beatmaps/osz/{beatmapset_id}.osz"


def osu_key(beatmap_id: int) -> str:
    return f"beatmaps/osu/{beatmap_id}.osu"
