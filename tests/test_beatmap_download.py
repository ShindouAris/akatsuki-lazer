"""Tests for the .osz download mirrors fallback of BeatmapService."""

from collections.abc import AsyncIterator
from collections.abc import Callable

import httpx
import pytest

from app.services.beatmaps import BeatmapService

Outcome = Callable[[], httpx.Response] | Exception


# The service reads in chunks of 8192 bytes and only hands over full chunks, so a smaller first chunk would
# still be sitting in httpx's buffer (never delivered to the caller) when the connection dies.
FIRST_CHUNK = b"a" * 8192


class _DroppedStream(httpx.AsyncByteStream):
    """A 200 response whose connection dies after the first full chunk."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield FIRST_CHUNK
        raise httpx.ReadError("connection dropped")


def _ok(body: bytes = b"osz-bytes") -> Outcome:
    return lambda: httpx.Response(200, content=body)


def _status(code: int) -> Outcome:
    return lambda: httpx.Response(code)


def _service(outcomes: dict[str, Outcome], requested: list[str]) -> BeatmapService:
    """A service whose mirrors answer as described by ``outcomes`` (keyed by host)."""

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        outcome = outcomes[request.url.host]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome()

    service = BeatmapService(db=None)
    service._get_download_client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handle),
        follow_redirects=True,
    )
    return service


async def _download(service: BeatmapService, no_video: bool = False) -> bytes:
    return b"".join([chunk async for chunk in service.download_beatmapset(118, no_video)])


async def test_first_mirror_is_used_when_it_works() -> None:
    requested: list[str] = []
    service = _service({"catboy.best": _ok(b"from-catboy")}, requested)

    assert await _download(service) == b"from-catboy"
    assert requested == ["https://catboy.best/d/118"]


async def test_next_mirror_is_used_when_the_first_refuses() -> None:
    requested: list[str] = []
    service = _service(
        {"catboy.best": _status(403), "mirror.hinamizawa.ai": _ok(b"from-hinamizawa")},
        requested,
    )

    assert await _download(service) == b"from-hinamizawa"
    assert requested == ["https://catboy.best/d/118", "https://mirror.hinamizawa.ai/api/d/118"]


async def test_each_mirror_gets_its_own_no_video_url() -> None:
    requested: list[str] = []
    service = _service(
        {
            "catboy.best": _status(403),
            "mirror.hinamizawa.ai": _status(404),  # no no-video variant: asked for the full file
            "mirror.nekoha.moe": _ok(b"from-nekoha"),
        },
        requested,
    )

    assert await _download(service, no_video=True) == b"from-nekoha"
    assert requested == [
        "https://catboy.best/d/118n",
        "https://mirror.hinamizawa.ai/api/d/118",
        "https://mirror.nekoha.moe/api/download/118?noVideo=1",
    ]


async def test_connection_error_before_the_first_byte_falls_back() -> None:
    requested: list[str] = []
    service = _service(
        {
            "catboy.best": httpx.ConnectError("unreachable"),
            "mirror.hinamizawa.ai": _ok(b"from-hinamizawa"),
        },
        requested,
    )

    assert await _download(service) == b"from-hinamizawa"


async def test_error_after_the_first_byte_is_not_retried_on_another_mirror() -> None:
    requested: list[str] = []
    service = _service(
        {
            "catboy.best": lambda: httpx.Response(200, stream=_DroppedStream()),
            "mirror.hinamizawa.ai": _ok(b"must-not-be-appended"),
        },
        requested,
    )

    received: list[bytes] = []
    with pytest.raises(httpx.ReadError):
        async for chunk in service.download_beatmapset(118):
            received.append(chunk)

    assert received == [FIRST_CHUNK]
    assert requested == ["https://catboy.best/d/118"]


async def test_failure_lists_why_every_mirror_failed() -> None:
    service = _service(
        {
            "catboy.best": _status(403),
            "mirror.hinamizawa.ai": _status(404),
            "mirror.nekoha.moe": httpx.ConnectError("unreachable"),
        },
        [],
    )

    with pytest.raises(Exception, match="beatmapset 118") as error:
        await _download(service)

    message = str(error.value)
    assert "catboy.best: HTTP 403" in message
    assert "mirror.hinamizawa.ai: HTTP 404" in message
    assert "mirror.nekoha.moe: ConnectError" in message
