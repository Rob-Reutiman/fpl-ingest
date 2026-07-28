"""Shared data shapes — the contracts passed between FPL Edge modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Player:
    """A player as it appears in the bootstrap-static element list."""

    code: int                    # Permanent cross-season identifier
    fpl_id: int                  # This-season ID (joins to picks)
    web_name: str
    team: int
    position: int                # 1=GK, 2=DEF, 3=MID, 4=FWD
    now_cost: int                # In 0.1m units
    selected_by_percent: float


@dataclass
class Pick:
    """A single squad selection for a manager in a given gameweek."""

    manager_id: int
    gameweek: int
    fpl_id: int
    position: int                # Squad slot 1-15
    multiplier: int              # 0=benched, 1=playing, 2=captain, 3=TC
    is_captain: bool
    is_vice_captain: bool
    active_chip: str | None      # freehit, wildcard, bboost, 3xc, None


@dataclass
class Transfer:
    """A single transfer made by a manager in a given gameweek."""

    manager_id: int
    gameweek: int
    element_in: int              # fpl_id of player transferred in
    element_out: int             # fpl_id of player transferred out
    element_in_cost: int
    element_out_cost: int
    time: str                    # ISO timestamp
