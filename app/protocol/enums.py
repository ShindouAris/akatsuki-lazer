"""Enums matching official osu! client definitions."""

from enum import IntEnum


class SpectatedUserState(IntEnum):
    """State of a spectated user."""

    IDLE = 0
    PLAYING = 1
    PAUSED = 2
    PASSED = 3
    FAILED = 4
    QUIT = 5


class MultiplayerUserState(IntEnum):
    """State of a user in a multiplayer room."""

    IDLE = 0
    READY = 1
    WAITING_FOR_LOAD = 2
    LOADED = 3
    READY_FOR_GAMEPLAY = 4
    PLAYING = 5
    FINISHED_PLAY = 6
    RESULTS = 7
    SPECTATING = 8


class MultiplayerRoomState(IntEnum):
    """State of a multiplayer room."""

    OPEN = 0
    WAITING_FOR_LOAD = 1
    PLAYING = 2
    CLOSED = 3


class MatchType(IntEnum):
    """Type of multiplayer match."""

    PLAYLISTS = 0
    HEAD_TO_HEAD = 1
    TEAM_VERSUS = 2
    MATCHMAKING = 3


class QueueMode(IntEnum):
    """Queue mode for multiplayer playlist."""

    HOST_ONLY = 0
    ALL_PLAYERS = 1
    ALL_PLAYERS_ROUND_ROBIN = 2


class DownloadState(IntEnum):
    """Beatmap download state."""

    UNKNOWN = 0
    NOT_DOWNLOADED = 1
    DOWNLOADING = 2
    IMPORTING = 3
    LOCALLY_AVAILABLE = 4


class UserStatus(IntEnum):
    """User online status."""

    OFFLINE = 0
    DO_NOT_DISTURB = 1
    ONLINE = 2


class HitResult(IntEnum):
    """Hit result types for statistics."""

    NONE = 0
    MISS = 1
    MEH = 2
    OK = 3
    GOOD = 4
    GREAT = 5
    PERFECT = 6
    SMALL_TICK_MISS = 7
    SMALL_TICK_HIT = 8
    LARGE_TICK_MISS = 9
    LARGE_TICK_HIT = 10
    SMALL_BONUS = 11
    LARGE_BONUS = 12
    IGNORE_MISS = 13
    IGNORE_HIT = 14
    COMBO_BREAK = 15
    SLIDER_TAIL_HIT = 16
    LEGACY_COMBO_INCREASE = 99


# String keys used in JSON API (snake_case)
HIT_RESULT_NAMES = {
    HitResult.NONE: "none",
    HitResult.MISS: "miss",
    HitResult.MEH: "meh",
    HitResult.OK: "ok",
    HitResult.GOOD: "good",
    HitResult.GREAT: "great",
    HitResult.PERFECT: "perfect",
    HitResult.SMALL_TICK_MISS: "small_tick_miss",
    HitResult.SMALL_TICK_HIT: "small_tick_hit",
    HitResult.LARGE_TICK_MISS: "large_tick_miss",
    HitResult.LARGE_TICK_HIT: "large_tick_hit",
    HitResult.SMALL_BONUS: "small_bonus",
    HitResult.LARGE_BONUS: "large_bonus",
    HitResult.IGNORE_MISS: "ignore_miss",
    HitResult.IGNORE_HIT: "ignore_hit",
    HitResult.COMBO_BREAK: "combo_break",
    HitResult.SLIDER_TAIL_HIT: "slider_tail_hit",
    HitResult.LEGACY_COMBO_INCREASE: "legacy_combo_increase",
}

HIT_RESULT_FROM_NAME = {v: k for k, v in HIT_RESULT_NAMES.items()}


class ReplayButtonState(IntEnum):
    """Replay button state flags."""

    NONE = 0
    LEFT1 = 1
    RIGHT1 = 2
    LEFT2 = 4
    RIGHT2 = 8
    SMOKE = 16


# UserActivity union type identifiers
class UserActivityType(IntEnum):
    """Union type identifiers for UserActivity subclasses."""

    CHOOSING_BEATMAP = 11
    IN_SOLO_GAME = 12
    WATCHING_REPLAY = 13
    SPECTATING_USER = 14
    SEARCHING_FOR_LOBBY = 21
    IN_LOBBY = 22
    IN_MULTIPLAYER_GAME = 23
    SPECTATING_MULTIPLAYER_GAME = 24
    IN_PLAYLIST_GAME = 31
    EDITING_BEATMAP = 41
    MODDING_BEATMAP = 42
    TESTING_BEATMAP = 43
    IN_DAILY_CHALLENGE_LOBBY = 51
    PLAYING_DAILY_CHALLENGE = 52
