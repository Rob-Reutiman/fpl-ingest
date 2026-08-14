"""Resolves a season's `element_id` to a `player_master_id` stable across seasons.

FPL reassigns `element` ids every season, so a query spanning two of them needs
a durable key to join on.

Matching runs in tiers, and their order is the whole design.

  0. `player_code`, the Premier League player code, which survives a rollover.
  1. Normalized name, matched exactly after lowercasing and stripping accents.
  2. Normalized name matching several masters, resolved by team continuity.
  3. Nothing matched, so a fresh master id.

The code leads because FPL relists names between seasons. Across the 2023 to
2026 seasons that covers 66 players, Rodri and Kepa among them, whose careers
name matching alone would split in two.

Splitting one career in two beats fusing two careers into one. A spurious id
shows up in the review file and is fixable, whereas a wrong merge is silent.
Every decision resting on a name reaches `player_match_review.csv` with its
outcome, leaving the file to hold the cases a human can act on.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

_WHITESPACE = re.compile(r"\s+")


def normalize_name_key(first_name: str, second_name: str) -> str:
    """Return `"first second"`, lowercased, deaccented and whitespace collapsed.

    NFKD splits an accented character into a base and a combining mark, so
    dropping the marks turns "Håland" into "haland" with no lookup table.
    """
    combined = unicodedata.normalize("NFKD", f"{first_name} {second_name}")
    stripped = "".join(ch for ch in combined if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", stripped).strip().lower()


@dataclass(frozen=True)
class SeasonPlayer:
    """One season's view of a player, as read from the source."""

    season: str
    element_id: int
    player_code: int | None
    first_name: str
    second_name: str
    web_name: str
    team_master_id: str

    @property
    def name_key(self) -> str:
        return normalize_name_key(self.first_name, self.second_name)


@dataclass
class MasterPlayer:
    player_master_id: int
    player_code: int | None
    canonical_first_name: str
    canonical_second_name: str
    canonical_web_name: str
    normalized_name_key: str
    first_seen_season: str
    last_seen_season: str
    seasons: dict[str, str] = field(default_factory=dict)
    """Maps season to team_master_id. Held in memory for tier 2."""

    def as_row(self) -> dict[str, object]:
        return {
            "player_master_id": self.player_master_id,
            "player_code": self.player_code,
            "canonical_first_name": self.canonical_first_name,
            "canonical_second_name": self.canonical_second_name,
            "canonical_web_name": self.canonical_web_name,
            "normalized_name_key": self.normalized_name_key,
            "first_seen_season": self.first_seen_season,
            "last_seen_season": self.last_seen_season,
        }


@dataclass(frozen=True)
class ReviewRow:
    """One line of `player_match_review.csv`. `resolution` awaits a human."""

    season: str
    element_id: int
    web_name: str
    first_name: str
    second_name: str
    team_short: str
    candidate_master_ids: str
    match_method: str
    confidence: str
    resolution: str = ""


REVIEW_COLUMNS = (
    "season",
    "element_id",
    "web_name",
    "first_name",
    "second_name",
    "team_short",
    "candidate_master_ids",
    "match_method",
    "confidence",
    "resolution",
)

NAME_MATCH = "name_exact"
TEAM_CONTINUITY = "name_ambiguous_team_continuity"
NAME_CONFLICT = "name_match_rejected_different_code"
UNRESOLVED = "unresolved_new_master_id"


class MasterRegistry:
    """Accumulates `dim_player_master` and `map_player_season` across seasons.

    Resolve seasons oldest first. Ids then accrete chronologically and
    `first_seen_season` holds the season it names.
    """

    def __init__(self, existing: Iterable[MasterPlayer] = ()) -> None:
        self._masters: dict[int, MasterPlayer] = {m.player_master_id: m for m in existing}
        self._by_code: dict[int, int] = {
            m.player_code: m.player_master_id for m in self._masters.values() if m.player_code
        }
        self._by_name: dict[str, list[int]] = {}
        for master in self._masters.values():
            self._by_name.setdefault(master.normalized_name_key, []).append(master.player_master_id)
        self._next_id = max(self._masters, default=0) + 1
        self.review: list[ReviewRow] = []
        self.new_ids_by_season: dict[str, int] = {}

    @property
    def masters(self) -> list[MasterPlayer]:
        return sorted(self._masters.values(), key=lambda m: m.player_master_id)

    def resolve_season(self, players: Sequence[SeasonPlayer]) -> dict[int, int]:
        """Resolve one season's squads. Returns `element_id` to master id.

        Iterates in `element_id` order, so a rerun allocates ids identically.
        One master id may be claimed by one player per season.
        """
        assigned: dict[int, int] = {}
        claimed: set[int] = set()
        for player in sorted(players, key=lambda p: p.element_id):
            master_id = self._resolve_one(player, claimed)
            assigned[player.element_id] = master_id
            claimed.add(master_id)
        return assigned

    def _resolve_one(self, player: SeasonPlayer, claimed: set[int]) -> int:
        if player.player_code is not None:
            match = self._by_code.get(player.player_code)
            if match is not None:
                self._merge_season(match, player)
                return match

        candidates = [
            master_id
            for master_id in self._by_name.get(player.name_key, ())
            if master_id not in claimed
        ]

        # Two players sharing a name while holding different codes are two
        # different people, whatever the name says.
        if player.player_code is not None:
            conflicting = [
                master_id
                for master_id in candidates
                if self._masters[master_id].player_code not in (None, player.player_code)
            ]
            candidates = [c for c in candidates if c not in conflicting]
            if conflicting and not candidates:
                self._record(player, conflicting, NAME_CONFLICT, "rejected")
                return self._create(player)

        if len(candidates) == 1:
            match = candidates[0]
            self._record(player, [match], NAME_MATCH, "high")
            self._merge_season(match, player)
            return match

        if len(candidates) > 1:
            narrowed = [
                master_id
                for master_id in candidates
                if player.team_master_id in self._masters[master_id].seasons.values()
            ]
            if len(narrowed) == 1:
                match = narrowed[0]
                self._record(player, candidates, TEAM_CONTINUITY, "medium")
                self._merge_season(match, player)
                return match
            # A fresh id is wrong and visible. A guess would be wrong and silent.
            self._record(player, candidates, UNRESOLVED, "low")
            return self._create(player)

        # No candidates. Holding a code, this player is simply new to the master
        # table, and the code is authority enough to say so. Lacking one, the
        # decision rests on nothing and goes to the review file.
        if player.player_code is None:
            self._record(player, [], UNRESOLVED, "none")
        return self._create(player)

    def _create(self, player: SeasonPlayer) -> int:
        master_id = self._next_id
        self._next_id += 1
        master = MasterPlayer(
            player_master_id=master_id,
            player_code=player.player_code,
            canonical_first_name=player.first_name,
            canonical_second_name=player.second_name,
            canonical_web_name=player.web_name,
            normalized_name_key=player.name_key,
            first_seen_season=player.season,
            last_seen_season=player.season,
            seasons={player.season: player.team_master_id},
        )
        self._masters[master_id] = master
        if player.player_code is not None:
            self._by_code[player.player_code] = master_id
        self._by_name.setdefault(player.name_key, []).append(master_id)
        self.new_ids_by_season[player.season] = self.new_ids_by_season.get(player.season, 0) + 1
        return master_id

    def _merge_season(self, master_id: int, player: SeasonPlayer) -> None:
        """Fold one season's view of a player into their master row.

        Canonical names track the most recent season, holding the name the
        player goes by today.
        """
        master = self._masters[master_id]
        master.seasons[player.season] = player.team_master_id
        if player.season >= master.last_seen_season:
            master.last_seen_season = player.season
            master.canonical_first_name = player.first_name
            master.canonical_second_name = player.second_name
            master.canonical_web_name = player.web_name
            if master.normalized_name_key != player.name_key:
                self._by_name.setdefault(player.name_key, []).append(master_id)
                master.normalized_name_key = player.name_key
        if player.season < master.first_seen_season:
            master.first_seen_season = player.season
        if master.player_code is None and player.player_code is not None:
            master.player_code = player.player_code
            self._by_code[player.player_code] = master_id

    def _record(
        self, player: SeasonPlayer, candidates: Sequence[int], method: str, confidence: str
    ) -> None:
        self.review.append(
            ReviewRow(
                season=player.season,
                element_id=player.element_id,
                web_name=player.web_name,
                first_name=player.first_name,
                second_name=player.second_name,
                team_short=player.team_master_id,
                candidate_master_ids="|".join(str(c) for c in candidates),
                match_method=method,
                confidence=confidence,
            )
        )
