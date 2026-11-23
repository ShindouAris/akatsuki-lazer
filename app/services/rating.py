"""Ratings of ranked play, rated like osu-server-spectator does.

The reference server rates every match with a Plackett-Luce model (mu 1500, sigma 150, beta 0, tau 15) in
which each player is a team of one and the "score" that orders them is the life they have left, so that is
what :func:`rate_match` takes. Ratings are kept at the osu! scale; nothing is converted.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

from openskill.models import PlackettLuce

logger = logging.getLogger(__name__)

DEFAULT_MU = 1500.0
DEFAULT_SIGMA = 150.0
MIN_MU = 600.0


@dataclass(frozen=True)
class Elo:
    """A player's rating: the mean skill and how unsure the system is about it."""

    mu: float = DEFAULT_MU
    sigma: float = DEFAULT_SIGMA


def _gamma(*_args: object) -> float:
    return 1.0


_model = PlackettLuce(mu=DEFAULT_MU, sigma=DEFAULT_SIGMA, beta=0, tau=15, gamma=_gamma)


def initial_rating_from_pp(pp: float) -> Elo:
    """Rating of a player who has never played ranked play, estimated from their performance points."""
    estimate = -4000 + 600 * math.log(max(pp, 0.0) + 4000)
    return Elo(mu=max(MIN_MU, estimate), sigma=DEFAULT_SIGMA)


def rate_match(players: dict[int, Elo], lives: dict[int, int]) -> dict[int, Elo]:
    """Rate a finished match. A player with more life left placed better; equal life is a draw.

    Args:
        players: every participant's rating before the match, by user id.
        lives: the life every participant had left, by user id.

    Returns:
        every participant's rating after the match.
    """
    if not players:
        return {}

    user_ids = list(players)
    teams = [[_model.rating(mu=players[uid].mu, sigma=players[uid].sigma)] for uid in user_ids]
    rated = _model.rate(teams, scores=[lives.get(uid, 0) for uid in user_ids])

    return {
        uid: Elo(mu=max(MIN_MU, team[0].mu), sigma=team[0].sigma)
        for uid, team in zip(user_ids, rated, strict=True)
    }


def parse_elo_data(elo_data: str | None) -> Elo:
    """Read the JSON stored in ``matchmaking_user_stats.elo_data``.

    A missing or broken value is a new player.
    """
    try:
        data = json.loads(elo_data or "{}")
        # The reference server calls the deviation "sig"; "sigma" is accepted too.
        return Elo(
            mu=float(data.get("mu", DEFAULT_MU)),
            sigma=float(data.get("sig", data.get("sigma", DEFAULT_SIGMA))),
        )
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        logger.warning("Unreadable elo_data %r, treating the player as new", elo_data)
        return Elo()


def serialize_elo_data(elo: Elo, initial: Elo | None = None, contests: int = 0) -> str:
    """The JSON stored in ``matchmaking_user_stats.elo_data``."""
    data: dict[str, float | int] = {
        "mu": round(elo.mu, 4),
        "sig": round(elo.sigma, 4),
        "contests": contests,
    }
    if initial is not None:
        data["initial_mu"] = round(initial.mu, 4)
    return json.dumps(data)


def initial_in(elo_data: str | None) -> Elo | None:
    """The rating the player started with, if it was recorded."""
    try:
        data = json.loads(elo_data or "{}")
        return Elo(mu=float(data["initial_mu"])) if "initial_mu" in data else None
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None


def contests_in(elo_data: str | None) -> int:
    """How many rated matches the stored rating has seen."""
    try:
        return int(json.loads(elo_data or "{}").get("contests", 0))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return 0


def card_rating(star_rating: float) -> int:
    """Initial rating of a pool beatmap that has no learned rating: harder maps are rated higher."""
    return round(800 + 500 * (math.exp(0.16 * star_rating) - 1))
