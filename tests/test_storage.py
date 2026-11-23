"""The storage chain: S3 first, SeaweedFS as the fallback, the local disk last."""

from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from app.core.config import Settings
from app.services.replay import ReplayStorageService
from app.services.storage import LocalBackend
from app.services.storage import ObjectStorage
from app.services.storage import S3Backend
from app.services.storage import StorageError
from app.services.storage import build_storage
from app.services.storage import replay_key


class FakeBackend:
    """In memory; `down` makes every call fail like an unreachable server."""

    def __init__(self, name: str, *, down: bool = False) -> None:
        self.name = name
        self.down = down
        self.files: dict[str, bytes] = {}

    def _check(self) -> None:
        if self.down:
            raise ConnectionError(f"{self.name} is unreachable")

    async def put_bytes(self, key: str, data: bytes) -> None:
        self._check()
        self.files[key] = data

    async def put_file(self, key: str, path: Path) -> None:
        self._check()
        self.files[key] = path.read_bytes()

    async def get_bytes(self, key: str) -> bytes | None:
        self._check()
        return self.files.get(key)

    async def download_to(self, key: str, path: Path) -> bool:
        self._check()
        if key not in self.files:
            return False
        path.write_bytes(self.files[key])
        return True

    async def exists(self, key: str) -> bool:
        self._check()
        return key in self.files


@pytest.mark.asyncio
async def test_writes_go_to_the_first_backend_that_answers() -> None:
    s3, seaweed = FakeBackend("s3"), FakeBackend("seaweedfs")
    storage = ObjectStorage([s3, seaweed])

    assert await storage.put_bytes("replays/1.osr", b"a") == "s3"
    assert s3.files == {"replays/1.osr": b"a"} and seaweed.files == {}


@pytest.mark.asyncio
async def test_a_write_falls_back_to_seaweedfs_when_s3_is_down() -> None:
    s3, seaweed = FakeBackend("s3", down=True), FakeBackend("seaweedfs")
    storage = ObjectStorage([s3, seaweed])

    assert await storage.put_bytes("replays/2.osr", b"b") == "seaweedfs"
    assert seaweed.files == {"replays/2.osr": b"b"}


@pytest.mark.asyncio
async def test_a_write_fails_loudly_when_every_backend_is_down() -> None:
    storage = ObjectStorage([FakeBackend("s3", down=True), FakeBackend("seaweedfs", down=True)])

    with pytest.raises(StorageError, match="s3: ConnectionError; seaweedfs: ConnectionError"):
        await storage.put_bytes("replays/3.osr", b"c")


@pytest.mark.asyncio
async def test_reads_look_in_the_fallback_when_s3_is_down_or_does_not_have_the_file() -> None:
    s3, seaweed = FakeBackend("s3", down=True), FakeBackend("seaweedfs")
    seaweed.files["replays/4.osr"] = b"d"
    storage = ObjectStorage([s3, seaweed])

    assert await storage.get_bytes("replays/4.osr") == b"d", "S3 is down"
    s3.down = False
    assert await storage.get_bytes("replays/4.osr") == b"d", "S3 is up but does not have it"
    assert await storage.get_bytes("replays/nope.osr") is None
    assert await storage.exists("replays/4.osr") and not await storage.exists("replays/nope.osr")


@pytest.mark.asyncio
async def test_downloads_fall_back_too(tmp_path: Path) -> None:
    s3, seaweed = FakeBackend("s3", down=True), FakeBackend("seaweedfs")
    seaweed.files["beatmaps/osu/5.osu"] = b"osu file"
    storage = ObjectStorage([s3, seaweed])

    assert await storage.download_to("beatmaps/osu/5.osu", tmp_path / "5.osu")
    assert (tmp_path / "5.osu").read_bytes() == b"osu file"
    assert not await storage.download_to("beatmaps/osu/6.osu", tmp_path / "6.osu")


@pytest.mark.asyncio
async def test_a_backend_that_just_failed_is_skipped_for_a_while_then_tried_again() -> None:
    now = [0.0]
    calls: list[str] = []

    class Counting(FakeBackend):
        async def put_bytes(self, key: str, data: bytes) -> None:
            calls.append(self.name)
            await super().put_bytes(key, data)

    s3, seaweed = Counting("s3", down=True), Counting("seaweedfs")
    storage = ObjectStorage([s3, seaweed], cooldown_seconds=30, clock=lambda: now[0])

    await storage.put_bytes("replays/1.osr", b"a")
    await storage.put_bytes("replays/2.osr", b"b")
    assert calls == ["s3", "seaweedfs", "seaweedfs"], "S3 is not asked again right after it failed"

    now[0] = 31.0
    s3.down = False
    await storage.put_bytes("replays/3.osr", b"c")
    assert calls[-1] == "s3", "after the cooldown S3 is tried again, and takes the write once it is back"


@pytest.mark.asyncio
async def test_a_backend_that_hangs_counts_as_failed() -> None:
    import asyncio

    class Hanging(FakeBackend):
        async def put_bytes(self, key: str, data: bytes) -> None:
            await asyncio.sleep(30)

    fallback = FakeBackend("seaweedfs")
    storage = ObjectStorage([Hanging("s3"), fallback], quick_timeout=0.05)

    assert await storage.put_bytes("replays/1.osr", b"a") == "seaweedfs"


@pytest.mark.asyncio
async def test_the_last_backend_is_never_skipped() -> None:
    only = FakeBackend("local", down=True)
    storage = ObjectStorage([only], cooldown_seconds=30, clock=lambda: 0.0)

    with pytest.raises(StorageError):
        await storage.put_bytes("replays/1.osr", b"a")
    only.down = False
    assert await storage.put_bytes("replays/1.osr", b"a") == "local"


def test_the_chain_follows_the_settings() -> None:
    nothing = build_storage(Settings())
    assert [b.name for b in nothing.backends] == ["local"]

    both = build_storage(
        Settings(s3_bucket="osu", seaweedfs_endpoint_url="http://seaweedfs:8333", seaweedfs_bucket="osu"),
    )
    assert [b.name for b in both.backends] == ["s3", "seaweedfs", "local"]

    only_seaweed = build_storage(Settings(seaweedfs_endpoint_url="http://seaweedfs:8333"))
    assert [b.name for b in only_seaweed.backends] == ["seaweedfs", "local"]


def _stubbed_backend() -> tuple[S3Backend, Stubber]:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s",
        config=Config(retries={"max_attempts": 0}),
    )
    backend = S3Backend(
        name="s3", bucket="osu", endpoint_url="", access_key="k", secret_key="s", region="us-east-1", timeout=1,
        client=client,
    )
    return backend, Stubber(client)


@pytest.mark.asyncio
async def test_s3_reports_a_missing_key_as_none_and_a_server_error_as_a_failure() -> None:
    backend, stub = _stubbed_backend()
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    stub.add_client_error("get_object", service_error_code="InternalError", http_status_code=500)

    with stub:
        assert await backend.get_bytes("replays/7.osr") is None
        with pytest.raises(Exception, match="InternalError"):
            await backend.get_bytes("replays/7.osr")


@pytest.mark.asyncio
async def test_s3_creates_a_missing_bucket_before_the_first_write() -> None:
    backend, stub = _stubbed_backend()
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    stub.add_response("create_bucket", {}, {"Bucket": "osu"})
    stub.add_response("put_object", {}, {"Bucket": "osu", "Key": "replays/8.osr", "Body": b"x"})

    with stub:
        await backend.put_bytes("replays/8.osr", b"x")
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_local_files_keep_the_old_layout(tmp_path: Path) -> None:
    settings = Settings(replays_path=str(tmp_path / "replays"), beatmaps_path=str(tmp_path / "beatmaps"))
    local = LocalBackend(settings)

    await local.put_bytes("replays/9.osr", b"r")
    await local.put_bytes("beatmaps/osu/9.osu", b"o")

    assert (tmp_path / "replays" / "9.osr").read_bytes() == b"r"
    assert (tmp_path / "beatmaps" / "osu" / "9.osu").read_bytes() == b"o"


@pytest.mark.asyncio
async def test_a_replay_is_read_back_through_the_chain() -> None:
    primary, fallback = FakeBackend("s3", down=True), FakeBackend("seaweedfs")
    service = ReplayStorageService(ObjectStorage([primary, fallback]))

    # No frames means nothing to store.
    assert await service.persist_score_replay(
        score_id=1, username="u", beatmap_checksum="c", ruleset_id=0, ended_at=None, build_id=None, total_score=1,
        max_combo=1, beatmap_max_combo=1, statistics={}, mods=[], frame_bundles=[],
    ) is None

    fallback.files[replay_key(11)] = b"stored elsewhere"
    assert await service.get_score_replay(11) == b"stored elsewhere"
    assert await service.get_score_replay(12) is None
