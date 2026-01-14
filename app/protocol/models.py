"""Protocol models for osu! client communication.

These models serialize to MessagePack arrays with integer keys, matching the
official osu! server format. Each model has a `to_msgpack()` method that returns
a list/tuple where the index corresponds to the [Key(n)] attribute in C#.

Example:
    SpectatorState with [Key(0)] BeatmapID, [Key(1)] RulesetID, etc.
    serializes to: [beatmap_id, ruleset_id, mods, state, maximum_statistics]
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import Any

from app.protocol.enums import HIT_RESULT_FROM_NAME
from app.protocol.enums import HIT_RESULT_NAMES
from app.protocol.enums import DownloadState
from app.protocol.enums import HitResult
from app.protocol.enums import MatchType
from app.protocol.enums import MultiplayerRoomState
from app.protocol.enums import MultiplayerUserState
from app.protocol.enums import QueueMode
from app.protocol.enums import ReplayButtonState
from app.protocol.enums import SpectatedUserState
from app.protocol.enums import UserActivityType
from app.protocol.enums import UserStatus


def _stats_to_msgpack(stats: dict[str, int]) -> dict[int, int]:
    """Convert string-keyed statistics to HitResult int keys."""
    result = {}
    for key, value in stats.items():
        if isinstance(key, str):
            hit_result = HIT_RESULT_FROM_NAME.get(key)
            if hit_result is not None:
                result[int(hit_result)] = value
        elif isinstance(key, int):
            result[key] = value
    return result


def _stats_from_msgpack(stats: dict[int, int] | dict[str, int]) -> dict[str, int]:
    """Convert HitResult int keys to string-keyed statistics."""
    result = {}
    for key, value in stats.items():
        if isinstance(key, int):
            name = HIT_RESULT_NAMES.get(HitResult(key), str(key))
            result[name] = value
        else:
            result[key] = value
    return result


# =============================================================================
# Spectator Hub Models
# =============================================================================


@dataclass
class APIMod:
    """Mod configuration.

    MessagePack format: [acronym, settings]
    Keys: [0] Acronym, [1] Settings
    """

    acronym: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [self.acronym, self.settings]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> APIMod:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                acronym=data[0] if len(data) > 0 else "",
                settings=data[1] if len(data) > 1 else {},
            )
        return cls(
            acronym=data.get("acronym", ""),
            settings=data.get("settings", {}),
        )

    @classmethod
    def from_list(cls, mods: list[dict | list]) -> list[APIMod]:
        """Convert a list of mod data to APIMod objects."""
        return [cls.from_msgpack(m) for m in mods]


@dataclass
class SpectatorState:
    """State of a spectated user's gameplay session.

    MessagePack format: [beatmap_id, ruleset_id, mods, state, maximum_statistics]
    Keys: [0] BeatmapID, [1] RulesetID, [2] Mods, [3] State, [4] MaximumStatistics
    """

    beatmap_id: int | None = None
    ruleset_id: int | None = None
    mods: list[APIMod] = field(default_factory=list)
    state: SpectatedUserState = SpectatedUserState.IDLE
    maximum_statistics: dict[str, int] = field(default_factory=dict)

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.beatmap_id,  # Key 0
            self.ruleset_id,  # Key 1
            [m.to_msgpack() for m in self.mods],  # Key 2
            int(self.state),  # Key 3
            _stats_to_msgpack(self.maximum_statistics),  # Key 4
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> SpectatorState:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            mods_data = data[2] if len(data) > 2 else []
            stats_data = data[4] if len(data) > 4 else {}
            return cls(
                beatmap_id=data[0] if len(data) > 0 else None,
                ruleset_id=data[1] if len(data) > 1 else None,
                mods=APIMod.from_list(mods_data) if mods_data else [],
                state=SpectatedUserState(data[3]) if len(data) > 3 else SpectatedUserState.IDLE,
                maximum_statistics=_stats_from_msgpack(stats_data),
            )
        # Handle dict format (from JSON)
        mods_data = data.get("mods", [])
        return cls(
            beatmap_id=data.get("beatmapId") or data.get("beatmap_id"),
            ruleset_id=data.get("rulesetId") or data.get("ruleset_id"),
            mods=APIMod.from_list(mods_data) if mods_data else [],
            state=SpectatedUserState(data.get("state", 0)),
            maximum_statistics=data.get("maximumStatistics") or data.get("maximum_statistics") or {},
        )


@dataclass
class ScoreProcessorStatistics:
    """Score processor statistics for accurate score calculation.

    MessagePack format: [base_score, maximum_base_score, accuracy_judgement_count,
    combo_portion, bonus_portion]
    Keys: [0] BaseScore, [1] MaximumBaseScore, [2] AccuracyJudgementCount, [3] ComboPortion, [4] BonusPortion
    """

    base_score: float = 0.0
    maximum_base_score: float = 0.0
    accuracy_judgement_count: int = 0
    combo_portion: float = 0.0
    bonus_portion: float = 0.0

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.base_score,  # Key 0
            self.maximum_base_score,  # Key 1
            self.accuracy_judgement_count,  # Key 2
            self.combo_portion,  # Key 3
            self.bonus_portion,  # Key 4
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict | None) -> ScoreProcessorStatistics:
        """Deserialize from MessagePack array or dict format."""
        if data is None:
            return cls()
        if isinstance(data, list):
            return cls(
                base_score=data[0] if len(data) > 0 else 0.0,
                maximum_base_score=data[1] if len(data) > 1 else 0.0,
                accuracy_judgement_count=data[2] if len(data) > 2 else 0,
                combo_portion=data[3] if len(data) > 3 else 0.0,
                bonus_portion=data[4] if len(data) > 4 else 0.0,
            )
        return cls(
            base_score=data.get("baseScore") or data.get("base_score") or 0.0,
            maximum_base_score=data.get("maximumBaseScore") or data.get("maximum_base_score") or 0.0,
            accuracy_judgement_count=data.get("accuracyJudgementCount") or data.get(
                "accuracy_judgement_count",
            ) or 0,
            combo_portion=data.get("comboPortion") or data.get("combo_portion") or 0.0,
            bonus_portion=data.get("bonusPortion") or data.get("bonus_portion") or 0.0,
        )


@dataclass
class FrameHeader:
    """Header for replay frame bundle.

    MessagePack format: [total_score, accuracy, combo, max_combo, statistics,
    score_processor_statistics, received_time, mods]
    Keys: [0] TotalScore, [1] Accuracy, [2] Combo, [3] MaxCombo, [4] Statistics,
          [5] ScoreProcessorStatistics, [6] ReceivedTime, [7] Mods
    """

    total_score: int = 0
    accuracy: float = 0.0
    combo: int = 0
    max_combo: int = 0
    statistics: dict[str, int] = field(default_factory=dict)
    score_processor_statistics: ScoreProcessorStatistics = field(default_factory=ScoreProcessorStatistics)
    received_time: datetime | None = None
    mods: list[APIMod] | None = None  # Nullable for backwards compatibility

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        # Format received_time as ISO string or use current time
        received_time_str = (
            self.received_time.isoformat() if self.received_time else datetime.utcnow().isoformat()
        )
        return [
            self.total_score,  # Key 0
            self.accuracy,  # Key 1
            self.combo,  # Key 2
            self.max_combo,  # Key 3
            _stats_to_msgpack(self.statistics),  # Key 4
            self.score_processor_statistics.to_msgpack(),  # Key 5
            received_time_str,  # Key 6
            [m.to_msgpack() for m in self.mods] if self.mods is not None else None,  # Key 7
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> FrameHeader:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            stats_data = data[4] if len(data) > 4 else {}
            sps_data = data[5] if len(data) > 5 else None
            mods_data = data[7] if len(data) > 7 else None
            return cls(
                total_score=data[0] if len(data) > 0 else 0,
                accuracy=data[1] if len(data) > 1 else 0.0,
                combo=data[2] if len(data) > 2 else 0,
                max_combo=data[3] if len(data) > 3 else 0,
                statistics=_stats_from_msgpack(stats_data),
                score_processor_statistics=ScoreProcessorStatistics.from_msgpack(sps_data),
                received_time=None,  # Will be set by server
                mods=APIMod.from_list(mods_data) if mods_data is not None else None,
            )
        stats_data = data.get("statistics") or {}
        sps_data = data.get("scoreProcessorStatistics") or data.get("score_processor_statistics")
        mods_data = data.get("mods")
        return cls(
            total_score=data.get("totalScore") or data.get("total_score") or 0,
            accuracy=data.get("accuracy") or 0.0,
            combo=data.get("combo") or 0,
            max_combo=data.get("maxCombo") or data.get("max_combo") or 0,
            statistics=stats_data,
            score_processor_statistics=ScoreProcessorStatistics.from_msgpack(sps_data),
            received_time=None,
            mods=APIMod.from_list(mods_data) if mods_data is not None else None,
        )


@dataclass
class LegacyReplayFrame:
    """Legacy replay frame data.

    MessagePack format: [time, mouse_x, mouse_y, button_state]
    Keys: [0] Time (from base class), [1] MouseX, [2] MouseY, [3] ButtonState
    """

    time: float = 0.0
    mouse_x: float | None = None
    mouse_y: float | None = None
    button_state: ReplayButtonState = ReplayButtonState.NONE

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.time,  # Key 0
            self.mouse_x,  # Key 1
            self.mouse_y,  # Key 2
            int(self.button_state),  # Key 3
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> LegacyReplayFrame:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                time=data[0] if len(data) > 0 else 0.0,
                mouse_x=data[1] if len(data) > 1 else None,
                mouse_y=data[2] if len(data) > 2 else None,
                button_state=ReplayButtonState(data[3]) if len(data) > 3 else ReplayButtonState.NONE,
            )
        return cls(
            time=data.get("time") or 0.0,
            mouse_x=data.get("mouseX") or data.get("mouse_x"),
            mouse_y=data.get("mouseY") or data.get("mouse_y"),
            button_state=ReplayButtonState(data.get("buttonState") or data.get("button_state") or 0),
        )


@dataclass
class FrameDataBundle:
    """Bundle of replay frames.

    MessagePack format: [header, frames]
    Keys: [0] Header, [1] Frames
    """

    header: FrameHeader = field(default_factory=FrameHeader)
    frames: list[LegacyReplayFrame] = field(default_factory=list)

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.header.to_msgpack(),  # Key 0
            [f.to_msgpack() for f in self.frames],  # Key 1
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> FrameDataBundle:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            header_data = data[0] if len(data) > 0 else {}
            frames_data = data[1] if len(data) > 1 else []
            return cls(
                header=FrameHeader.from_msgpack(header_data),
                frames=[LegacyReplayFrame.from_msgpack(f) for f in frames_data],
            )
        return cls(
            header=FrameHeader.from_msgpack(data.get("header") or {}),
            frames=[LegacyReplayFrame.from_msgpack(f) for f in data.get("frames") or []],
        )


@dataclass
class SpectatorUser:
    """User info for spectating.

    MessagePack format: [online_id, username]
    Keys: [0] OnlineID, [1] Username
    """

    online_id: int = 0
    username: str = ""

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.online_id,  # Key 0
            self.username,  # Key 1
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> SpectatorUser:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                online_id=data[0] if len(data) > 0 else 0,
                username=data[1] if len(data) > 1 else "",
            )
        return cls(
            online_id=data.get("onlineId") or data.get("online_id") or data.get("userId") or 0,
            username=data.get("username") or "",
        )


# =============================================================================
# Multiplayer Hub Models
# =============================================================================


@dataclass
class BeatmapAvailability:
    """Beatmap availability state.

    MessagePack format: [state, download_progress]
    Keys: [0] State, [1] DownloadProgress
    """

    state: DownloadState = DownloadState.UNKNOWN
    download_progress: float | None = None

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            int(self.state),  # Key 0
            self.download_progress,  # Key 1
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict | None) -> BeatmapAvailability:
        """Deserialize from MessagePack array or dict format."""
        if data is None:
            return cls()
        if isinstance(data, list):
            return cls(
                state=DownloadState(data[0]) if len(data) > 0 else DownloadState.UNKNOWN,
                download_progress=data[1] if len(data) > 1 else None,
            )
        return cls(
            state=DownloadState(data.get("state", 0)),
            download_progress=data.get("downloadProgress") or data.get("download_progress"),
        )

    @classmethod
    def unknown(cls) -> BeatmapAvailability:
        return cls(DownloadState.UNKNOWN)

    @classmethod
    def locally_available(cls) -> BeatmapAvailability:
        return cls(DownloadState.LOCALLY_AVAILABLE)


@dataclass
class MultiplayerRoomUser:
    """User in a multiplayer room.

    MessagePack format: [user_id, state, beatmap_availability, mods, match_state,
    ruleset_id, beatmap_id, voted_to_skip_intro]
    Keys: [0] UserID, [1] State, [2] BeatmapAvailability, [3] Mods, [4] MatchState,
          [5] RulesetId, [6] BeatmapId, [7] VotedToSkipIntro
    """

    user_id: int = 0
    state: MultiplayerUserState = MultiplayerUserState.IDLE
    beatmap_availability: BeatmapAvailability = field(default_factory=BeatmapAvailability)
    mods: list[APIMod] = field(default_factory=list)
    match_state: Any | None = None  # MatchUserState - complex union type
    ruleset_id: int | None = None
    beatmap_id: int | None = None
    voted_to_skip_intro: bool = False

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.user_id,  # Key 0
            int(self.state),  # Key 1
            self.beatmap_availability.to_msgpack(),  # Key 2
            [m.to_msgpack() for m in self.mods],  # Key 3
            self.match_state,  # Key 4 - pass through as-is for now
            self.ruleset_id,  # Key 5
            self.beatmap_id,  # Key 6
            self.voted_to_skip_intro,  # Key 7
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerRoomUser:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            mods_data = data[3] if len(data) > 3 else []
            avail_data = data[2] if len(data) > 2 else None
            return cls(
                user_id=data[0] if len(data) > 0 else 0,
                state=MultiplayerUserState(data[1]) if len(data) > 1 else MultiplayerUserState.IDLE,
                beatmap_availability=BeatmapAvailability.from_msgpack(avail_data),
                mods=APIMod.from_list(mods_data) if mods_data else [],
                match_state=data[4] if len(data) > 4 else None,
                ruleset_id=data[5] if len(data) > 5 else None,
                beatmap_id=data[6] if len(data) > 6 else None,
                voted_to_skip_intro=data[7] if len(data) > 7 else False,
            )
        mods_data = data.get("mods", [])
        avail_data = data.get("beatmapAvailability") or data.get("beatmap_availability")
        return cls(
            user_id=data.get("userId") or data.get("user_id") or 0,
            state=MultiplayerUserState(data.get("state", 0)),
            beatmap_availability=BeatmapAvailability.from_msgpack(avail_data),
            mods=APIMod.from_list(mods_data) if mods_data else [],
            match_state=data.get("matchState") or data.get("match_state"),
            ruleset_id=data.get("rulesetId") or data.get("ruleset_id"),
            beatmap_id=data.get("beatmapId") or data.get("beatmap_id"),
            voted_to_skip_intro=data.get("votedToSkipIntro") or data.get("voted_to_skip_intro") or False,
        )


@dataclass
class MultiplayerRoomSettings:
    """Settings for a multiplayer room.

    MessagePack format: [name, playlist_item_id, password, match_type, queue_mode,
    auto_start_duration, auto_skip]
    Keys: [0] Name, [1] PlaylistItemId, [2] Password, [3] MatchType, [4] QueueMode,
          [5] AutoStartDuration, [6] AutoSkip
    """

    name: str = "Unnamed room"
    playlist_item_id: int = 0
    password: str = ""
    match_type: MatchType = MatchType.HEAD_TO_HEAD
    queue_mode: QueueMode = QueueMode.HOST_ONLY
    auto_start_duration: timedelta = field(default_factory=lambda: timedelta(0))
    auto_skip: bool = False

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        # TimeSpan serializes as ticks (100-nanosecond intervals) or total seconds
        # Using ISO 8601 duration format for compatibility
        duration_ticks = int(self.auto_start_duration.total_seconds() * 10_000_000)
        return [
            self.name,  # Key 0
            self.playlist_item_id,  # Key 1
            self.password,  # Key 2
            int(self.match_type),  # Key 3
            int(self.queue_mode),  # Key 4
            duration_ticks,  # Key 5 - TimeSpan as ticks
            self.auto_skip,  # Key 6
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerRoomSettings:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            # Parse TimeSpan from ticks
            duration_ticks = data[5] if len(data) > 5 else 0
            duration = timedelta(microseconds=duration_ticks / 10) if duration_ticks else timedelta(0)
            return cls(
                name=data[0] if len(data) > 0 else "Unnamed room",
                playlist_item_id=data[1] if len(data) > 1 else 0,
                password=data[2] if len(data) > 2 else "",
                match_type=MatchType(data[3]) if len(data) > 3 else MatchType.HEAD_TO_HEAD,
                queue_mode=QueueMode(data[4]) if len(data) > 4 else QueueMode.HOST_ONLY,
                auto_start_duration=duration,
                auto_skip=data[6] if len(data) > 6 else False,
            )
        # Handle dict format
        duration_val = data.get("autoStartDuration") or data.get("auto_start_duration") or 0
        if isinstance(duration_val, (int, float)):
            duration = timedelta(seconds=duration_val)
        else:
            duration = timedelta(0)
        return cls(
            name=data.get("name") or "Unnamed room",
            playlist_item_id=data.get("playlistItemId") or data.get("playlist_item_id") or 0,
            password=data.get("password") or "",
            match_type=MatchType(data.get("matchType") or data.get("match_type") or 1),
            queue_mode=QueueMode(data.get("queueMode") or data.get("queue_mode") or 0),
            auto_start_duration=duration,
            auto_skip=data.get("autoSkip") or data.get("auto_skip") or False,
        )


@dataclass
class MultiplayerPlaylistItem:
    """Playlist item in a multiplayer room.

    MessagePack format: [id, owner_id, beatmap_id, beatmap_checksum, ruleset_id, required_mods,
                        allowed_mods, expired, playlist_order, played_at, star_rating, freestyle]
    Keys: [0-11] as listed above
    """

    id: int = 0
    owner_id: int = 0
    beatmap_id: int = 0
    beatmap_checksum: str = ""
    ruleset_id: int = 0
    required_mods: list[APIMod] = field(default_factory=list)
    allowed_mods: list[APIMod] = field(default_factory=list)
    expired: bool = False
    playlist_order: int = 0
    played_at: datetime | None = None
    star_rating: float = 0.0
    freestyle: bool = False

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        played_at_str = self.played_at.isoformat() if self.played_at else None
        return [
            self.id,  # Key 0
            self.owner_id,  # Key 1
            self.beatmap_id,  # Key 2
            self.beatmap_checksum,  # Key 3
            self.ruleset_id,  # Key 4
            [m.to_msgpack() for m in self.required_mods],  # Key 5
            [m.to_msgpack() for m in self.allowed_mods],  # Key 6
            self.expired,  # Key 7
            self.playlist_order,  # Key 8
            played_at_str,  # Key 9
            self.star_rating,  # Key 10
            self.freestyle,  # Key 11
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerPlaylistItem:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            required_mods = data[5] if len(data) > 5 else []
            allowed_mods = data[6] if len(data) > 6 else []
            return cls(
                id=data[0] if len(data) > 0 else 0,
                owner_id=data[1] if len(data) > 1 else 0,
                beatmap_id=data[2] if len(data) > 2 else 0,
                beatmap_checksum=data[3] if len(data) > 3 else "",
                ruleset_id=data[4] if len(data) > 4 else 0,
                required_mods=APIMod.from_list(required_mods),
                allowed_mods=APIMod.from_list(allowed_mods),
                expired=data[7] if len(data) > 7 else False,
                playlist_order=data[8] if len(data) > 8 else 0,
                played_at=None,  # Parse from ISO string if needed
                star_rating=data[10] if len(data) > 10 else 0.0,
                freestyle=data[11] if len(data) > 11 else False,
            )
        required_mods = data.get("requiredMods") or data.get("required_mods") or []
        allowed_mods = data.get("allowedMods") or data.get("allowed_mods") or []
        return cls(
            id=data.get("id") or 0,
            owner_id=data.get("ownerId") or data.get("owner_id") or 0,
            beatmap_id=data.get("beatmapId") or data.get("beatmap_id") or 0,
            beatmap_checksum=data.get("beatmapChecksum") or data.get("beatmap_checksum") or "",
            ruleset_id=data.get("rulesetId") or data.get("ruleset_id") or 0,
            required_mods=APIMod.from_list(required_mods),
            allowed_mods=APIMod.from_list(allowed_mods),
            expired=data.get("expired") or False,
            playlist_order=data.get("playlistOrder") or data.get("playlist_order") or 0,
            played_at=None,
            star_rating=data.get("starRating") or data.get("star_rating") or 0.0,
            freestyle=data.get("freestyle") or False,
        )


@dataclass
class MultiplayerRoom:
    """A multiplayer room.

    MessagePack format: [room_id, state, settings, users, host, match_state, playlist,
    active_countdowns, channel_id]
    Keys: [0] RoomID, [1] State, [2] Settings, [3] Users, [4] Host, [5] MatchState,
          [6] Playlist, [7] ActiveCountdowns, [8] ChannelID
    """

    room_id: int = 0
    state: MultiplayerRoomState = MultiplayerRoomState.OPEN
    settings: MultiplayerRoomSettings = field(default_factory=MultiplayerRoomSettings)
    users: list[MultiplayerRoomUser] = field(default_factory=list)
    host: MultiplayerRoomUser | None = None
    match_state: Any | None = None  # MatchRoomState - complex union type
    playlist: list[MultiplayerPlaylistItem] = field(default_factory=list)
    active_countdowns: list[Any] = field(default_factory=list)  # MultiplayerCountdown
    channel_id: int = 0

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.room_id,  # Key 0
            int(self.state),  # Key 1
            self.settings.to_msgpack(),  # Key 2
            [u.to_msgpack() for u in self.users],  # Key 3
            self.host.to_msgpack() if self.host else None,  # Key 4
            self.match_state,  # Key 5 - pass through as-is
            [p.to_msgpack() for p in self.playlist],  # Key 6
            self.active_countdowns,  # Key 7 - pass through as-is
            self.channel_id,  # Key 8
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerRoom:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            settings_data = data[2] if len(data) > 2 else {}
            users_data = data[3] if len(data) > 3 else []
            host_data = data[4] if len(data) > 4 else None
            playlist_data = data[6] if len(data) > 6 else []
            return cls(
                room_id=data[0] if len(data) > 0 else 0,
                state=MultiplayerRoomState(data[1]) if len(data) > 1 else MultiplayerRoomState.OPEN,
                settings=MultiplayerRoomSettings.from_msgpack(settings_data),
                users=[MultiplayerRoomUser.from_msgpack(u) for u in users_data],
                host=MultiplayerRoomUser.from_msgpack(host_data) if host_data else None,
                match_state=data[5] if len(data) > 5 else None,
                playlist=[MultiplayerPlaylistItem.from_msgpack(p) for p in playlist_data],
                active_countdowns=data[7] if len(data) > 7 else [],
                channel_id=data[8] if len(data) > 8 else 0,
            )
        settings_data = data.get("settings") or {}
        users_data = data.get("users") or []
        host_data = data.get("host")
        playlist_data = data.get("playlist") or []
        return cls(
            room_id=data.get("roomId") or data.get("room_id") or 0,
            state=MultiplayerRoomState(data.get("state", 0)),
            settings=MultiplayerRoomSettings.from_msgpack(settings_data),
            users=[MultiplayerRoomUser.from_msgpack(u) for u in users_data],
            host=MultiplayerRoomUser.from_msgpack(host_data) if host_data else None,
            match_state=data.get("matchState") or data.get("match_state"),
            playlist=[MultiplayerPlaylistItem.from_msgpack(p) for p in playlist_data],
            active_countdowns=data.get("activeCountdowns") or data.get("active_countdowns") or [],
            channel_id=data.get("channelId") or data.get("channel_id") or 0,
        )


# =============================================================================
# Metadata Hub Models
# =============================================================================


@dataclass
class BeatmapUpdates:
    """Beatmap update notification.

    MessagePack format: [beatmap_set_ids, last_processed_queue_id]
    Keys: [0] BeatmapSetIDs, [1] LastProcessedQueueID
    """

    beatmap_set_ids: list[int] = field(default_factory=list)
    last_processed_queue_id: int = 0

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.beatmap_set_ids,  # Key 0
            self.last_processed_queue_id,  # Key 1
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> BeatmapUpdates:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                beatmap_set_ids=data[0] if len(data) > 0 else [],
                last_processed_queue_id=data[1] if len(data) > 1 else 0,
            )
        return cls(
            beatmap_set_ids=data.get("beatmapSetIds") or data.get("beatmap_set_ids") or [],
            last_processed_queue_id=data.get("lastProcessedQueueId") or data.get(
                "last_processed_queue_id",
            ) or 0,
        )


@dataclass
class UserPresence:
    """User presence information.

    MessagePack format: [activity, status]
    Keys: [0] Activity, [1] Status
    """

    activity: UserActivity | None = None
    status: UserStatus | None = None

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.activity.to_msgpack() if self.activity else None,  # Key 0
            int(self.status) if self.status is not None else None,  # Key 1
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict | None) -> UserPresence | None:
        """Deserialize from MessagePack array or dict format."""
        if data is None:
            return None
        if isinstance(data, list):
            activity_data = data[0] if len(data) > 0 else None
            status_val = data[1] if len(data) > 1 else None
            return cls(
                activity=UserActivity.from_msgpack(activity_data) if activity_data else None,
                status=UserStatus(status_val) if status_val is not None else None,
            )
        activity_data = data.get("activity")
        status_val = data.get("status")
        return cls(
            activity=UserActivity.from_msgpack(activity_data) if activity_data else None,
            status=UserStatus(status_val) if status_val is not None else None,
        )


# =============================================================================
# UserActivity Union Types
# =============================================================================


@dataclass
class UserActivity:
    """Base class for user activity.

    This is a union type in MessagePack. The format is:
    [type_id, data_array]

    Where type_id identifies the subclass and data_array contains the fields.
    """

    # Common fields for InGame activities
    beatmap_id: int = 0
    beatmap_display_title: str = ""
    ruleset_id: int = 0
    ruleset_playing_verb: str = ""

    # Fields for WatchingReplay/SpectatingUser
    score_id: int = 0
    player_name: str = ""

    # Fields for InLobby
    room_id: int = 0
    room_name: str = ""

    # Type identifier
    activity_type: UserActivityType | None = None

    def to_msgpack(self) -> list | None:
        """Serialize to MessagePack union format: [type_id, data]."""
        if self.activity_type is None:
            return None

        type_id = int(self.activity_type)

        # ChoosingBeatmap, SearchingForLobby, InDailyChallengeLobby have no fields
        if self.activity_type in (
            UserActivityType.CHOOSING_BEATMAP,
            UserActivityType.SEARCHING_FOR_LOBBY,
            UserActivityType.IN_DAILY_CHALLENGE_LOBBY,
        ):
            return [type_id, []]

        # InGame types: [beatmap_id, beatmap_display_title, ruleset_id, ruleset_playing_verb]
        if self.activity_type in (
            UserActivityType.IN_SOLO_GAME,
            UserActivityType.IN_MULTIPLAYER_GAME,
            UserActivityType.IN_PLAYLIST_GAME,
            UserActivityType.SPECTATING_MULTIPLAYER_GAME,
            UserActivityType.PLAYING_DAILY_CHALLENGE,
        ):
            return [
                type_id,
                [
                    self.beatmap_id,
                    self.beatmap_display_title,
                    self.ruleset_id,
                    self.ruleset_playing_verb,
                ],
            ]

        # EditingBeatmap types: [beatmap_id, beatmap_display_title]
        if self.activity_type in (
            UserActivityType.EDITING_BEATMAP,
            UserActivityType.MODDING_BEATMAP,
            UserActivityType.TESTING_BEATMAP,
        ):
            return [
                type_id,
                [
                    self.beatmap_id,
                    self.beatmap_display_title,
                ],
            ]

        # WatchingReplay/SpectatingUser: [score_id, player_name, beatmap_id, beatmap_display_title]
        if self.activity_type in (
            UserActivityType.WATCHING_REPLAY,
            UserActivityType.SPECTATING_USER,
        ):
            return [
                type_id,
                [
                    self.score_id,
                    self.player_name,
                    self.beatmap_id,
                    self.beatmap_display_title,
                ],
            ]

        # InLobby: [room_id, room_name]
        if self.activity_type == UserActivityType.IN_LOBBY:
            return [
                type_id,
                [
                    self.room_id,
                    self.room_name,
                ],
            ]

        return None

    @classmethod
    def from_msgpack(cls, data: list | dict | None) -> UserActivity | None:
        """Deserialize from MessagePack union format."""
        if data is None:
            return None

        # Handle union format: [type_id, data_array]
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[0], int):
            type_id = data[0]
            fields = data[1] if len(data) > 1 else []

            try:
                activity_type = UserActivityType(type_id)
            except ValueError:
                return None

            activity = cls(activity_type=activity_type)

            # Parse fields based on type
            if activity_type in (
                UserActivityType.IN_SOLO_GAME,
                UserActivityType.IN_MULTIPLAYER_GAME,
                UserActivityType.IN_PLAYLIST_GAME,
                UserActivityType.SPECTATING_MULTIPLAYER_GAME,
                UserActivityType.PLAYING_DAILY_CHALLENGE,
            ):
                if len(fields) > 0:
                    activity.beatmap_id = fields[0]
                if len(fields) > 1:
                    activity.beatmap_display_title = fields[1]
                if len(fields) > 2:
                    activity.ruleset_id = fields[2]
                if len(fields) > 3:
                    activity.ruleset_playing_verb = fields[3]

            elif activity_type in (
                UserActivityType.EDITING_BEATMAP,
                UserActivityType.MODDING_BEATMAP,
                UserActivityType.TESTING_BEATMAP,
            ):
                if len(fields) > 0:
                    activity.beatmap_id = fields[0]
                if len(fields) > 1:
                    activity.beatmap_display_title = fields[1]

            elif activity_type in (
                UserActivityType.WATCHING_REPLAY,
                UserActivityType.SPECTATING_USER,
            ):
                if len(fields) > 0:
                    activity.score_id = fields[0]
                if len(fields) > 1:
                    activity.player_name = fields[1]
                if len(fields) > 2:
                    activity.beatmap_id = fields[2]
                if len(fields) > 3:
                    activity.beatmap_display_title = fields[3]

            elif activity_type == UserActivityType.IN_LOBBY:
                if len(fields) > 0:
                    activity.room_id = fields[0]
                if len(fields) > 1:
                    activity.room_name = fields[1]

            return activity

        # Handle dict format (from JSON protocol or internal use)
        if isinstance(data, dict):
            activity_type_val = data.get("activityType") or data.get("activity_type")
            if activity_type_val is not None:
                try:
                    activity_type = UserActivityType(activity_type_val)
                except ValueError:
                    activity_type = None
            else:
                activity_type = None

            return cls(
                activity_type=activity_type,
                beatmap_id=data.get("beatmapId") or data.get("beatmap_id") or 0,
                beatmap_display_title=data.get("beatmapDisplayTitle") or data.get(
                    "beatmap_display_title",
                ) or "",
                ruleset_id=data.get("rulesetId") or data.get("ruleset_id") or 0,
                ruleset_playing_verb=data.get("rulesetPlayingVerb") or data.get("ruleset_playing_verb") or "",
                score_id=data.get("scoreId") or data.get("score_id") or 0,
                player_name=data.get("playerName") or data.get("player_name") or "",
                room_id=data.get("roomId") or data.get("room_id") or 0,
                room_name=data.get("roomName") or data.get("room_name") or "",
            )

        return None

    # Factory methods for creating specific activity types
    @classmethod
    def choosing_beatmap(cls) -> UserActivity:
        return cls(activity_type=UserActivityType.CHOOSING_BEATMAP)

    @classmethod
    def in_solo_game(
        cls,
        beatmap_id: int,
        beatmap_display_title: str,
        ruleset_id: int,
        ruleset_playing_verb: str,
    ) -> UserActivity:
        return cls(
            activity_type=UserActivityType.IN_SOLO_GAME,
            beatmap_id=beatmap_id,
            beatmap_display_title=beatmap_display_title,
            ruleset_id=ruleset_id,
            ruleset_playing_verb=ruleset_playing_verb,
        )

    @classmethod
    def in_lobby(cls, room_id: int, room_name: str) -> UserActivity:
        return cls(
            activity_type=UserActivityType.IN_LOBBY,
            room_id=room_id,
            room_name=room_name,
        )

    @classmethod
    def searching_for_lobby(cls) -> UserActivity:
        return cls(activity_type=UserActivityType.SEARCHING_FOR_LOBBY)


@dataclass
class DailyChallengeInfo:
    """Daily challenge information.

    MessagePack format: [room_id]
    Keys: [0] RoomID
    """

    room_id: int = 0

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [self.room_id]

    @classmethod
    def from_msgpack(cls, data: list | dict | None) -> DailyChallengeInfo | None:
        """Deserialize from MessagePack array or dict format."""
        if data is None:
            return None
        if isinstance(data, list):
            return cls(room_id=data[0] if len(data) > 0 else 0)
        return cls(room_id=data.get("roomId") or data.get("room_id") or 0)


@dataclass
class MultiplayerPlaylistItemStats:
    """Statistics for a multiplayer playlist item.

    MessagePack format: [playlist_item_id, total_score_distribution, cumulative_score,
    last_processed_score_id]
    Keys: [0] PlaylistItemID, [1] TotalScoreDistribution, [2] CumulativeScore, [3] LastProcessedScoreID
    """

    playlist_item_id: int = 0
    total_score_distribution: list[int] = field(default_factory=lambda: [0] * 13)  # 13 bins
    cumulative_score: int = 0
    last_processed_score_id: int = 0

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.playlist_item_id,  # Key 0
            self.total_score_distribution,  # Key 1
            self.cumulative_score,  # Key 2
            self.last_processed_score_id,  # Key 3
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerPlaylistItemStats:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                playlist_item_id=data[0] if len(data) > 0 else 0,
                total_score_distribution=data[1] if len(data) > 1 else [0] * 13,
                cumulative_score=data[2] if len(data) > 2 else 0,
                last_processed_score_id=data[3] if len(data) > 3 else 0,
            )
        return cls(
            playlist_item_id=data.get("playlistItemId") or data.get("playlist_item_id") or 0,
            total_score_distribution=data.get("totalScoreDistribution") or data.get(
                "total_score_distribution",
            ) or [0] * 13,
            cumulative_score=data.get("cumulativeScore") or data.get("cumulative_score") or 0,
            last_processed_score_id=data.get("lastProcessedScoreId") or data.get(
                "last_processed_score_id",
            ) or 0,
        )


@dataclass
class MultiplayerRoomScoreSetEvent:
    """Event when a score is set in a multiplayer room.

    MessagePack format: [room_id, playlist_item_id, score_id, user_id, total_score, new_rank]
    Keys: [0] RoomID, [1] PlaylistItemID, [2] ScoreID, [3] UserID, [4] TotalScore, [5] NewRank
    """

    room_id: int = 0
    playlist_item_id: int = 0
    score_id: int = 0
    user_id: int = 0
    total_score: int = 0
    new_rank: int | None = None

    def to_msgpack(self) -> list:
        """Serialize to MessagePack array format."""
        return [
            self.room_id,  # Key 0
            self.playlist_item_id,  # Key 1
            self.score_id,  # Key 2
            self.user_id,  # Key 3
            self.total_score,  # Key 4
            self.new_rank,  # Key 5
        ]

    @classmethod
    def from_msgpack(cls, data: list | dict) -> MultiplayerRoomScoreSetEvent:
        """Deserialize from MessagePack array or dict format."""
        if isinstance(data, list):
            return cls(
                room_id=data[0] if len(data) > 0 else 0,
                playlist_item_id=data[1] if len(data) > 1 else 0,
                score_id=data[2] if len(data) > 2 else 0,
                user_id=data[3] if len(data) > 3 else 0,
                total_score=data[4] if len(data) > 4 else 0,
                new_rank=data[5] if len(data) > 5 else None,
            )
        return cls(
            room_id=data.get("roomId") or data.get("room_id") or 0,
            playlist_item_id=data.get("playlistItemId") or data.get("playlist_item_id") or 0,
            score_id=data.get("scoreId") or data.get("score_id") or 0,
            user_id=data.get("userId") or data.get("user_id") or 0,
            total_score=data.get("totalScore") or data.get("total_score") or 0,
            new_rank=data.get("newRank") or data.get("new_rank"),
        )
