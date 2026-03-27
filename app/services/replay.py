"""Replay persistence service for streamed spectator frame bundles."""

from __future__ import annotations

import lzma
import logging
import struct
from hashlib import md5
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.protocol.models import FrameDataBundle
from app.services.pp import mods_to_bitwise

logger = logging.getLogger(__name__)


class ReplayStorageService:
    """Store and retrieve replay payload files for submitted scores."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_score_replay_path(self, score_id: int) -> Path:
        """Return the canonical replay file path for a score."""
        replay_dir = Path(self._settings.replays_path)
        return replay_dir / f"{score_id}.osr"

    async def persist_score_replay(
        self,
        *,
        score_id: int,
        username: str,
        beatmap_checksum: str | None,
        ruleset_id: int,
        ended_at: datetime | None,
        build_id: int | None,
        total_score: int,
        max_combo: int,
        beatmap_max_combo: int | None,
        statistics: dict[str, int],
        mods: list[dict[str, Any]],
        frame_bundles: list[FrameDataBundle],
    ) -> Path | None:
        """Persist replay frame bundles as a legacy-compatible .osr file."""
        if not frame_bundles:
            return None

        replay_data = _build_replay_data_string(frame_bundles)
        replay_payload = lzma.compress(replay_data.encode("utf-8"), format=lzma.FORMAT_ALONE)

        n300 = _pick_stat(statistics, "great", "count_300", "count300")
        n100 = _pick_stat(statistics, "ok", "count_100", "count100")
        n50 = _pick_stat(statistics, "meh", "count_50", "count50")
        ngeki = _pick_stat(statistics, "perfect", "count_geki", "countgeki")
        nkatu = _pick_stat(statistics, "good", "count_katu", "countkatu")
        nmiss = _pick_stat(statistics, "miss", "count_miss", "countmiss")
        mods_bitwise = mods_to_bitwise(mods)
        perfect = bool(beatmap_max_combo and max_combo >= beatmap_max_combo)

        beatmap_hash = beatmap_checksum or ""
        replay_hash = _build_replay_hash(
            beatmap_hash=beatmap_hash,
            username=username,
            total_score=total_score,
            max_combo=max_combo,
            mods_bitwise=mods_bitwise,
            replay_data=replay_data,
        )

        replay_path = self.get_score_replay_path(score_id)
        replay_path.parent.mkdir(parents=True, exist_ok=True)

        mode = ruleset_id if 0 <= ruleset_id <= 3 else 0
        version = _resolve_osu_version(build_id)
        timestamp_ticks = _datetime_to_ticks(ended_at or datetime.now(UTC))

        with replay_path.open("wb") as replay_file:
            replay_file.write(struct.pack("<BI", mode, version))
            _write_osr_string(replay_file, beatmap_hash)
            _write_osr_string(replay_file, username)
            _write_osr_string(replay_file, replay_hash)
            replay_file.write(
                struct.pack(
                    "<HHHHHHIH?I",
                    n300,
                    n100,
                    n50,
                    ngeki,
                    nkatu,
                    nmiss,
                    total_score,
                    max_combo,
                    perfect,
                    mods_bitwise,
                ),
            )
            _write_osr_string(replay_file, "")
            replay_file.write(struct.pack("<QI", timestamp_ticks, len(replay_payload)))
            replay_file.write(replay_payload)
            replay_file.write(struct.pack("<q", score_id))

        logger.info("Persisted replay .osr for score %s at %s", score_id, replay_path)
        return replay_path


def _pick_stat(statistics: dict[str, int], *keys: str) -> int:
    for key in keys:
        value = statistics.get(key)
        if isinstance(value, int):
            return max(0, value)
    return 0


def _resolve_osu_version(build_id: int | None) -> int:
    if build_id and 20_000_000 <= build_id <= 99_999_999:
        return build_id
    return int(datetime.now(UTC).strftime("%Y%m%d"))


def _datetime_to_ticks(value: datetime) -> int:
    utc_value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    # .osr timestamp uses .NET DateTime ticks (100ns since 0001-01-01 UTC).
    return int(utc_value.timestamp() * 10_000_000) + 621355968000000000


def _build_replay_hash(
    *,
    beatmap_hash: str,
    username: str,
    total_score: int,
    max_combo: int,
    mods_bitwise: int,
    replay_data: str,
) -> str:
    seed = f"{beatmap_hash}:{username}:{total_score}:{max_combo}:{mods_bitwise}:{replay_data}"
    return md5(seed.encode("utf-8"), usedforsecurity=False).hexdigest()


def _build_replay_data_string(frame_bundles: list[FrameDataBundle]) -> str:
    replay_rows: list[str] = []
    previous_time = 0
    has_frame = False

    for bundle in frame_bundles:
        for frame in bundle.frames:
            raw_time = int(round(frame.time))

            if has_frame:
                if raw_time >= previous_time:
                    delta = raw_time - previous_time
                    previous_time = raw_time
                else:
                    delta = max(0, raw_time)
                    previous_time += delta
            else:
                delta = max(0, raw_time)
                previous_time = delta
                has_frame = True

            x = float(frame.mouse_x) if frame.mouse_x is not None else 0.0
            y = float(frame.mouse_y) if frame.mouse_y is not None else 0.0
            z = int(frame.button_state)
            replay_rows.append(f"{delta}|{x:.6f}|{y:.6f}|{z}")

    replay_rows.append("-12345|0|0|0")
    return ",".join(replay_rows) + ","


def _write_osr_string(handle: Any, value: str) -> None:
    if not value:
        handle.write(b"\x00")
        return

    encoded = value.encode("utf-8")
    handle.write(b"\x0b")
    handle.write(_encode_uleb128(len(encoded)))
    handle.write(encoded)


def _encode_uleb128(value: int) -> bytes:
    encoded = bytearray()
    remaining = value
    while True:
        byte = remaining & 0x7F
        remaining >>= 7
        if remaining:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            break
    return bytes(encoded)
