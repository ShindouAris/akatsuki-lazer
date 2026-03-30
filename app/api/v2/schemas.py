"""Pydantic schemas for API responses."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


# User schemas
class UserCompact(BaseModel):
    """Minimal user information."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    avatar_url: str | None = None
    country_code: str = "XX"
    is_active: bool = True
    is_bot: bool = False
    is_supporter: bool = False


class RankHistoryResponse(BaseModel):
    """Rank history for displaying the rank graph."""

    mode: str = "osu"
    data: list[int] = Field(default_factory=list)


class UserStatisticsResponse(BaseModel):
    """User statistics for a game mode."""

    model_config = ConfigDict(from_attributes=True)

    ranked_score: int = 0
    total_score: int = 0
    pp: float = 0.0
    global_rank: int | None = None
    global_rank_percent: float | None = None  # Percentile ranking
    country_rank: int | None = None
    is_ranked: bool = False  # Whether user has any ranked plays
    rank_history: RankHistoryResponse | None = None  # For the rank graph
    accuracy: float = Field(alias="hit_accuracy", default=100.0)
    play_count: int = 0
    play_time: int = 0
    total_hits: int = 0
    maximum_combo: int = 0
    replays_watched_by_others: int = Field(alias="replays_watched", default=0)
    grade_counts: dict[str, int] = Field(default_factory=dict)
    level: dict[str, int] = Field(default_factory=dict)


class UserResponse(UserCompact):
    """Full user profile response."""

    cover_url: str | None = None
    title: str | None = None
    playmode: str = "osu"
    playstyle: list[str] | None = None
    is_restricted: bool = False
    created_at: datetime | None = Field(alias="join_date", default=None)
    last_visit: datetime | None = None
    statistics: UserStatisticsResponse | None = None



class Relation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_id: int
    relation_type: str  # "friend" or "block"
    mutual: bool = False
    target: UserCompact | None = None

class UserRelationResponse(BaseModel):
    """User relation response (friend or block)."""
    user_relation: Relation

# Beatmap schemas
class BeatmapCompact(BaseModel):
    """Minimal beatmap information."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    beatmapset_id: int
    version: str = Field(alias="version", default="")
    mode: int = 0
    mode_int: int = 0
    status: str = "pending"
    difficulty_rating: float = 0.0
    total_length: int = 0
    cs: float = 5.0
    ar: float = 5.0
    accuracy: float = Field( default=5.0)
    drain: float = Field( default=5.0)
    bpm: float = 0.0
    max_combo: int | None = None
    user_id: int = 0
    count_circles: int = 0
    count_sliders: int = 0
    count_spinners: int = 0
    checksum: str | None = None


class BeatmapsetCompact(BaseModel):
    """Minimal beatmapset information."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    artist: str
    artist_unicode: str | None = None
    title: str
    title_unicode: str | None = None
    creator: str
    user_id: int = 0
    status: str = "pending"
    play_count: int = 0
    favourite_count: int = 0


class BeatmapResponse(BeatmapCompact):
    """Full beatmap response."""

    beatmapset: BeatmapsetCompact | None = None


class GetBeatmapsResponse(BaseModel):
    """Response for bulk beatmap lookup endpoint."""

    beatmaps: list[BeatmapResponse] = Field(default_factory=list)


class BeatmapsetResponse(BeatmapsetCompact):
    """Full beatmapset response with beatmaps."""

    source: str | None = None
    tags: str | None = None
    ranked_date: datetime | None = None
    submitted_date: datetime | None = None
    last_updated: datetime | None = None
    bpm: float = 0.0
    preview_url: str | None = None
    has_video: bool = False
    has_storyboard: bool = False
    nsfw: bool = False
    beatmaps: list[BeatmapCompact] = Field(default_factory=list)


# Score schemas
class ModResponse(BaseModel):
    """Mod information."""

    acronym: str
    settings: dict[str, Any] = Field(default_factory=dict)


class ScoreResponse(BaseModel):
    """Score response (matches MultiplayerScore format)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    beatmap_id: int
    ruleset_id: int
    total_score: int
    accuracy: float
    pp: float | None = None
    max_combo: int
    rank: str
    passed: bool
    ranked: bool = True
    mods: list[ModResponse] = Field(default_factory=list)
    statistics: dict[str, int] = Field(default_factory=dict)
    maximum_statistics: dict[str, int] = Field(default_factory=dict)
    ended_at: datetime | None = None
    has_replay: bool = False
    rank_global: int | None = None  # Position on beatmap leaderboard
    position: int | None = None  # Compatibility alias for lazer score submit contract
    rank_country: int | None = None  # Position on country leaderboard
    user: UserCompact | None = None
    beatmap: BeatmapCompact | None = None


class ScoreSubmissionRequest(BaseModel):
    """Score submission request body."""

    accuracy: float
    max_combo: int
    mods: list[ModResponse] = Field(default_factory=list)
    passed: bool
    rank: str
    statistics: dict[str, int]
    maximum_statistics: dict[str, int] = Field(default_factory=dict)
    total_score: int
    total_score_without_mods: int = 0
    ruleset_id: int | None = None  # Client sends this, we use token's value
    pp: float | None = None
    pauses: list[int] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None  # Client doesn't send this in ForSubmission


class ScoreTokenResponse(BaseModel):
    """Score token response."""

    id: int
    created_at: datetime


# Multiplayer schemas
class MultiplayerPlaylistItemResponse(BaseModel):
    """Multiplayer playlist item response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    room_id: int
    beatmap_id: int
    ruleset_id: int
    required_mods: list[ModResponse] = Field(default_factory=list)
    allowed_mods: list[ModResponse] = Field(default_factory=list)
    playlist_order: int = 0
    played_at: datetime | None = None
    expired: bool = False
    beatmap: BeatmapCompact | None = None


class MultiplayerRoomResponse(BaseModel):
    """Multiplayer room response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: UserCompact | None = None
    type: str = "head_to_head"
    status: str = "idle"
    queue_mode: str = "host_only"
    max_participants: int = 16
    participant_count: int = 0
    auto_start_duration: int = 0
    auto_skip: bool = False
    category: str | None = None
    has_password: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    playlist: list[MultiplayerPlaylistItemResponse] = Field(default_factory=list)
    current_playlist_item: MultiplayerPlaylistItemResponse | None = None
    channel_id: int | None = None


class MultiplayerRoomCreateRequest(BaseModel):
    """Request to create a multiplayer room."""

    name: str
    password: str | None = None
    type: str = "head_to_head"
    queue_mode: str = "host_only"
    max_participants: int = 16
    auto_start_duration: int = 0
    auto_skip: bool = False
    category: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    playlist: list[dict[str, Any]] = Field(default_factory=list)


# User beatmap related schemas
class SoloScoreResponse(BaseModel):
    """Solo score response (full score with related objects)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    beatmap_id: int
    ruleset_id: int
    total_score: int
    accuracy: float
    pp: float | None = None
    max_combo: int
    rank: str
    passed: bool
    mods: list[ModResponse] = Field(default_factory=list)
    statistics: dict[str, int] = Field(default_factory=dict)
    ended_at: datetime | None = None
    has_replay: bool = False
    user: UserCompact | None = None
    beatmap: BeatmapCompact | None = None


class UserMostPlayedResponse(BaseModel):
    """User most played beatmap response."""

    model_config = ConfigDict(from_attributes=True)

    beatmap_id: int
    beatmapset_id: int
    count: int  # Number of times played
    beatmap: BeatmapCompact | None = None
    beatmapset: BeatmapsetCompact | None = None


class UserActivityResponse(BaseModel):
    """User activity response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    type: str  # "rank", "achievement", etc.
    beatmap_id: int | None = None
    mode: str = "osu"
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    beatmap: BeatmapCompact | None = None


class KudosuHistoryResponse(BaseModel):
    """Kudosu history response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    beatmap_id: int | None = None
    action: str  # "give", "reset", "deny"
    amount: int
    comment: str | None = None
    post_id: int | None = None
    created_at: datetime
    beatmap: BeatmapCompact | None = None

class Statistic(BaseModel):
    ssh: int = 0
    ss: int = 0
    sh: int = 0
    s: int = 0
    a: int = 0

class RankingUserEntryResponse(BaseModel):
    """Single user entry in a ranking list."""

    rank: int
    user: UserCompact
    pp: float = 0.0
    ranked_score: int = 0
    country_code: str = "XX"
    grade_counts: Statistic = Field(default_factory=Statistic)
    hit_accuracy: float = Field(alias="accuracy", default=100.0)
    play_count: int = 0


class RankingsResponse(BaseModel):
    """Paginated rankings response."""

    ranking: list[RankingUserEntryResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 50
    kind: str = "performance"


class UserScoreAggregateResponse(BaseModel):
    """Aggregate ranking info for a user's score."""

    score: ScoreResponse
    position: int
    user: UserCompact
