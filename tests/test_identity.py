"""Cross-season identity resolution.

The failure this suite exists to prevent is an over-merge: two players' careers
silently fused into one master id, which no downstream check would catch.
"""

from __future__ import annotations

from fpl.identity import (
    NAME_CONFLICT,
    NAME_MATCH,
    TEAM_CONTINUITY,
    UNRESOLVED,
    MasterRegistry,
    SeasonPlayer,
    normalize_name_key,
)


def make(
    element_id: int,
    first: str,
    second: str,
    *,
    season: str = "2023-24",
    code: int | None = 1000,
    team: str = "ARS",
) -> SeasonPlayer:
    return SeasonPlayer(
        season=season,
        element_id=element_id,
        player_code=code,
        first_name=first,
        second_name=second,
        web_name=second,
        team_master_id=team,
    )


class TestNormalizeNameKey:
    def test_lowercases_and_collapses_whitespace(self):
        assert normalize_name_key("  Bukayo ", " Saka  ") == "bukayo saka"

    def test_strips_diacritics(self):
        assert normalize_name_key("Erling", "Håland") == "erling haland"
        assert normalize_name_key("Rúben", "Días") == "ruben dias"

    def test_accented_and_plain_spellings_agree(self):
        assert normalize_name_key("Nicolás", "Jackson") == normalize_name_key("Nicolas", "Jackson")


class TestTierZeroCodeMatch:
    def test_same_code_resolves_to_one_master_across_seasons(self):
        registry = MasterRegistry()
        first = registry.resolve_season([make(1, "Rodrigo", "Hernandez", code=220566)])
        second = registry.resolve_season(
            [make(99, "Rodrigo", "Hernandez Cascante", season="2024-25", code=220566)]
        )
        assert first[1] == second[99]
        assert len(registry.masters) == 1

    def test_renamed_player_is_not_split(self):
        """The Rodri/Kepa/Merino case: FPL relists the name, the code holds.

        Name-only matching split 66 players like this across 2023-24 -> 2025-26.
        """
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Mikel", "Merino", code=195384)])
        registry.resolve_season([make(2, "Mikel", "Merino Zazon", season="2024-25", code=195384)])
        assert len(registry.masters) == 1
        assert registry.masters[0].canonical_second_name == "Merino Zazon"

    def test_code_match_is_not_sent_to_review(self):
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Bukayo", "Saka", code=7)])
        registry.resolve_season([make(2, "Bukayo", "Saka", season="2024-25", code=7)])
        assert registry.review == []

    def test_player_rejoining_after_a_missing_season_keeps_their_id(self):
        registry = MasterRegistry()
        first = registry.resolve_season([make(1, "Ivan", "Toney", code=500)])
        registry.resolve_season([make(2, "Other", "Player", season="2024-25", code=501)])
        third = registry.resolve_season([make(3, "Ivan", "Toney", season="2025-26", code=500)])
        assert first[1] == third[3]


class TestTierOneNameMatch:
    def test_missing_code_falls_back_to_the_name(self):
        registry = MasterRegistry()
        first = registry.resolve_season([make(1, "Cole", "Palmer", code=None)])
        second = registry.resolve_season([make(2, "Cole", "Palmer", season="2024-25", code=None)])
        assert first[1] == second[2]

    def test_diacritics_do_not_prevent_a_match(self):
        registry = MasterRegistry()
        first = registry.resolve_season([make(1, "Erling", "Håland", code=None)])
        second = registry.resolve_season(
            [make(2, "Erling", "Haaland", season="2024-25", code=None)]
        )
        # "Haaland" and "Håland" are different keys; only the accent is normalized.
        assert first[1] != second[2]

        third = registry.resolve_season([make(3, "Erling", "Haland", season="2025-26", code=None)])
        assert third[3] == first[1]

    def test_name_match_is_always_recorded_for_review(self):
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Cole", "Palmer", code=None)])
        registry.resolve_season([make(2, "Cole", "Palmer", season="2024-25", code=None)])
        assert [row.match_method for row in registry.review] == [UNRESOLVED, NAME_MATCH]


class TestOverMergeProtection:
    def test_namesakes_with_different_codes_are_kept_apart(self):
        """Two different people who happen to share a name.

        The stable code says they're different; the name says they're the same.
        The code wins, and the rejected match is recorded so it stays visible.
        """
        registry = MasterRegistry()
        first = registry.resolve_season([make(1, "Danny", "Ward", code=111)])
        second = registry.resolve_season([make(2, "Danny", "Ward", season="2024-25", code=222)])
        assert first[1] != second[2]
        assert len(registry.masters) == 2
        assert registry.review[-1].match_method == NAME_CONFLICT
        assert registry.review[-1].candidate_master_ids == str(first[1])

    def test_two_players_in_one_season_cannot_claim_the_same_master(self):
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Joe", "Gomez", code=None)])
        assigned = registry.resolve_season(
            [
                make(2, "Joe", "Gomez", season="2024-25", code=None),
                make(3, "Joe", "Gomez", season="2024-25", code=None),
            ]
        )
        assert assigned[2] != assigned[3]


class TestTierTwoTeamContinuity:
    def test_ambiguity_is_broken_by_the_club(self):
        registry = MasterRegistry()
        registry.resolve_season(
            [
                make(1, "Joe", "Gomez", code=None, team="ARS"),
                make(2, "Joe", "Gomez", code=None, team="CHE"),
            ]
        )
        assigned = registry.resolve_season(
            [make(3, "Joe", "Gomez", season="2024-25", code=None, team="CHE")]
        )
        assert assigned[3] == 2
        assert registry.review[-1].match_method == TEAM_CONTINUITY

    def test_unbreakable_ambiguity_gets_a_fresh_id_not_a_guess(self):
        registry = MasterRegistry()
        registry.resolve_season(
            [
                make(1, "Joe", "Gomez", code=None, team="ARS"),
                make(2, "Joe", "Gomez", code=None, team="CHE"),
            ]
        )
        assigned = registry.resolve_season(
            [make(3, "Joe", "Gomez", season="2024-25", code=None, team="EVE")]
        )
        assert assigned[3] == 3
        assert registry.review[-1].match_method == UNRESOLVED
        assert registry.review[-1].candidate_master_ids == "1|2"


class TestNewPlayers:
    def test_a_debutant_with_a_code_is_not_review_noise(self):
        """A code we haven't seen is a new player, not an unresolved match.

        Routing all ~1,400 of them to the review file would bury the handful of
        cases a human can act on.
        """
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Brand", "New", code=999)])
        assert registry.review == []
        assert len(registry.masters) == 1

    def test_a_debutant_without_a_code_is_recorded(self):
        registry = MasterRegistry()
        registry.resolve_season([make(1, "Brand", "New", code=None)])
        assert [row.match_method for row in registry.review] == [UNRESOLVED]

    def test_ids_are_allocated_in_element_id_order(self):
        registry = MasterRegistry()
        assigned = registry.resolve_season(
            [make(7, "C", "C", code=3), make(2, "A", "A", code=1), make(5, "B", "B", code=2)]
        )
        assert [assigned[2], assigned[5], assigned[7]] == [1, 2, 3]


class TestExistingMasters:
    def test_resuming_extends_rather_than_reassigns(self):
        first = MasterRegistry()
        first.resolve_season([make(1, "Bukayo", "Saka", code=7)])

        resumed = MasterRegistry(first.masters)
        assigned = resumed.resolve_season(
            [
                make(4, "Bukayo", "Saka", season="2024-25", code=7),
                make(5, "Brand", "New", season="2024-25", code=8),
            ]
        )
        assert assigned[4] == 1
        assert assigned[5] == 2
        assert len(resumed.masters) == 2

    def test_first_seen_season_survives_a_resume(self):
        first = MasterRegistry()
        first.resolve_season([make(1, "Bukayo", "Saka", code=7)])
        resumed = MasterRegistry(first.masters)
        resumed.resolve_season([make(4, "Bukayo", "Saka", season="2024-25", code=7)])
        master = resumed.masters[0]
        assert (master.first_seen_season, master.last_seen_season) == ("2023-24", "2024-25")
