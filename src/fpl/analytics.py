"""Analytics engine: Effective Ownership, ownership gap, and transfer flow."""

from __future__ import annotations

import polars as pl

from fpl.constants import POSITION_NAMES
from fpl.ingest.cohort import is_template_only
from fpl.storage import Storage

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

EO_SCHEMA: dict[str, pl.DataType] = {
    "fpl_id": pl.Int64(),
    "code": pl.Int64(),
    "web_name": pl.String(),
    "team_name": pl.String(),
    "position": pl.String(),
    "eo_top5k": pl.Float64(),
    "owned_pct_top5k": pl.Float64(),
    "captained_pct_top5k": pl.Float64(),
    "eo_broad": pl.Float64(),
    "owned_pct_broad": pl.Float64(),
    "captained_pct_broad": pl.Float64(),
    "eo_divergence": pl.Float64(),
    "cohort_size": pl.Int64(),
    "source": pl.String(),
}

GAP_SCHEMA: dict[str, pl.DataType] = {
    "fpl_id": pl.Int64(),
    "web_name": pl.String(),
    "team_name": pl.String(),
    "position": pl.String(),
    "now_cost": pl.Int64(),
    "effective_ownership": pl.Float64(),
    "ep_next": pl.Float64(),
    "news": pl.String(),
    "source": pl.String(),
    "next_1_opponent": pl.String(),
    "next_1_is_home": pl.Boolean(),
    "next_2_opponent": pl.String(),
    "next_2_is_home": pl.Boolean(),
    "next_3_opponent": pl.String(),
    "next_3_is_home": pl.Boolean(),
}

FLOW_SCHEMA: dict[str, pl.DataType] = {
    "fpl_id": pl.Int64(),
    "web_name": pl.String(),
    "team_name": pl.String(),
    "position": pl.String(),
    "now_cost": pl.Int64(),
    "transfer_count": pl.Int64(),
    "transfer_pct": pl.Float64(),
    "current_eo": pl.Float64(),
}


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _conform(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Select and cast to the output schema so every return path matches it."""
    return df.select(pl.col(name).cast(dtype) for name, dtype in schema.items())


# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------


def _player_meta(players: pl.DataFrame) -> pl.DataFrame:
    """Project dim_player rows to display metadata with named positions."""
    return players.select(
        "fpl_id",
        "code",
        "web_name",
        "team_name",
        pl.col("position").replace_strict(POSITION_NAMES).alias("position"),
    )


def _slice_eo(
    picks: pl.DataFrame, managers: pl.DataFrame, exclude_chip: str | None
) -> pl.DataFrame:
    """Per-slice ownership stats: one row per (fpl_id, is_top_slice).

    Managers on ``exclude_chip`` drop out of both the numerator (their picks)
    and the denominator (their slice size). Rates are fractions of the slice.
    """
    if exclude_chip is not None:
        excluded = picks.filter(pl.col("active_chip") == exclude_chip).select("manager_id").unique()
        managers = managers.join(excluded, on="manager_id", how="anti")
        picks = picks.join(excluded, on="manager_id", how="anti")

    slice_sizes = (
        managers.group_by("is_top_slice")
        .agg(pl.len().alias("slice_size"))
        .filter(pl.col("slice_size") > 0)
    )
    sliced = picks.join(managers.select("manager_id", "is_top_slice"), on="manager_id", how="inner")
    counts = sliced.group_by("fpl_id", "is_top_slice").agg(
        (pl.col("multiplier") >= 1).sum().alias("owned_n"),
        (pl.col("multiplier") == 2).sum().alias("cap_n"),
        (pl.col("multiplier") == 3).sum().alias("tc_n"),
    )
    # Densify against every existing slice so a player picked in only one slice
    # gets an explicit 0.0 (not null) in the other.
    grid = counts.select("fpl_id").unique().join(slice_sizes, how="cross")
    return (
        grid.join(counts, on=["fpl_id", "is_top_slice"], how="left")
        .with_columns(pl.col("owned_n", "cap_n", "tc_n").fill_null(0))
        .with_columns(
            (pl.col("owned_n") / pl.col("slice_size")).alias("owned_pct"),
            (pl.col("cap_n") / pl.col("slice_size")).alias("captained_pct"),
            (pl.col("tc_n") / pl.col("slice_size")).alias("tc_pct"),
        )
        .with_columns(
            (pl.col("owned_pct") + pl.col("captained_pct") + 2 * pl.col("tc_pct")).alias("eo")
        )
    )


def _pivot_slices(per_slice: pl.DataFrame) -> pl.DataFrame:
    """Widen per-slice rows into ``*_broad`` / ``*_top5k`` columns.

    When no top slice exists (GW 2-4) the top5k columns are null.
    """
    stats = ["eo", "owned_pct", "captained_pct"]

    def _slice(is_top: bool, suffix: str) -> pl.DataFrame:
        return per_slice.filter(pl.col("is_top_slice") == is_top).select(
            "fpl_id", *(pl.col(s).alias(f"{s}_{suffix}") for s in stats)
        )

    broad = _slice(False, "broad")
    if per_slice.filter(pl.col("is_top_slice")).is_empty():
        wide = broad.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"{s}_top5k") for s in stats)
    else:
        wide = broad.join(_slice(True, "top5k"), on="fpl_id", how="full", coalesce=True)
    return wide.with_columns((pl.col("eo_broad") - pl.col("eo_top5k")).alias("eo_divergence"))


def _template_eo(stats: pl.DataFrame, players: pl.DataFrame) -> pl.DataFrame:
    """EO from global ownership: broad columns carry selected_by_percent."""
    return (
        stats.select("fpl_id", "selected_by_percent")
        .join(_player_meta(players), on="fpl_id", how="inner")
        .with_columns(
            pl.col("selected_by_percent").cast(pl.Float64).alias("eo_broad"),
            pl.col("selected_by_percent").cast(pl.Float64).alias("owned_pct_broad"),
            pl.lit(0.0).alias("captained_pct_broad"),
            pl.lit(None, dtype=pl.Float64).alias("eo_top5k"),
            pl.lit(None, dtype=pl.Float64).alias("owned_pct_top5k"),
            pl.lit(None, dtype=pl.Float64).alias("captained_pct_top5k"),
            pl.lit(None, dtype=pl.Float64).alias("eo_divergence"),
            pl.lit(None, dtype=pl.Int64).alias("cohort_size"),
            pl.lit("template").alias("source"),
        )
    )


def _next_fixtures(fixtures: pl.DataFrame, teams: pl.DataFrame, n: int = 3) -> pl.DataFrame:
    """One row per team with its next ``n`` opponents: ``next_{i}_opponent`` /
    ``next_{i}_is_home``. Teams with fewer remaining fixtures get null tails."""
    per_team = pl.concat(
        [
            fixtures.select(
                "gameweek",
                "kickoff_time",
                pl.col("team_h").alias("team"),
                pl.col("team_a").alias("opponent_id"),
                pl.lit(True).alias("is_home"),
            ),
            fixtures.select(
                "gameweek",
                "kickoff_time",
                pl.col("team_a").alias("team"),
                pl.col("team_h").alias("opponent_id"),
                pl.lit(False).alias("is_home"),
            ),
        ]
    )
    ranked = (
        per_team.join(
            teams.select(pl.col("id").alias("opponent_id"), pl.col("short_name").alias("opponent")),
            on="opponent_id",
            how="left",
        )
        .sort("gameweek", "kickoff_time")
        .with_columns((pl.int_range(pl.len()).over("team") + 1).alias("rank"))
    )
    wide = per_team.select("team").unique()
    for i in range(1, n + 1):
        wide = wide.join(
            ranked.filter(pl.col("rank") == i).select(
                "team",
                pl.col("opponent").alias(f"next_{i}_opponent"),
                pl.col("is_home").alias(f"next_{i}_is_home"),
            ),
            on="team",
            how="left",
        )
    return wide


def _flow_counts(transfers: pl.DataFrame, direction_col: str, cohort_size: int) -> pl.DataFrame:
    """Count transfers by player for one direction, as count and % of cohort."""
    return (
        transfers.group_by(pl.col(direction_col).alias("fpl_id"))
        .agg(pl.len().alias("transfer_count"))
        .with_columns((pl.col("transfer_count") / cohort_size * 100).alias("transfer_pct"))
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_effective_ownership(
    storage: Storage,
    gw: int,
    exclude_chip: str | None = "freehit",
) -> pl.DataFrame:
    """Compute Effective Ownership for each player for a given GW.

    Template mode (GW <= 1, no cohort): broad columns carry global
    ``selected_by_percent`` verbatim (percent scale), captaincy is unknowable
    so ``captained_pct_broad`` is 0, top5k columns are null, ``cohort_size``
    is null, ``source`` is "template".

    Cohort mode (GW 2+): EO = owned + captained + 2 * triple-captained,
    computed independently per slice as fractions of that slice — the top
    slice and random slice are different populations sampled at different
    rates, so they are never pooled. Managers on ``exclude_chip`` are removed
    from both numerator and denominator of their slice. Triple-captaincy
    feeds the EO formula but is not an output column; ``captained_pct``
    counts regular captains (multiplier == 2) only. For GW 2-4 (no top
    slice) the top5k columns and ``eo_divergence`` are null.

    ``eo_divergence`` = eo_broad - eo_top5k: positive means a bandwagon the
    elite aren't buying, negative an early elite move worth attention.
    """
    if is_template_only(gw):
        stats = storage.get_player_gw_stats(gw)
        if stats.is_empty():
            return _empty(EO_SCHEMA)
        return _conform(_template_eo(stats, storage.get_players()), EO_SCHEMA)

    picks = storage.get_cohort_picks(gw)
    managers = storage.get_cohort_managers(gw)
    if picks.is_empty() or managers.is_empty():
        return _empty(EO_SCHEMA)

    per_slice = _slice_eo(picks, managers, exclude_chip)
    if per_slice.is_empty():
        return _empty(EO_SCHEMA)
    cohort_size = per_slice.select("is_top_slice", "slice_size").unique()["slice_size"].sum()
    eo = (
        _pivot_slices(per_slice)
        .join(_player_meta(storage.get_players()), on="fpl_id", how="left")
        .with_columns(
            pl.lit(cohort_size, dtype=pl.Int64).alias("cohort_size"),
            pl.lit("cohort").alias("source"),
        )
    )
    return _conform(eo, EO_SCHEMA)


def compute_ownership_gap(
    storage: Storage,
    gw: int,
    eo_threshold: float = 5.0,
) -> pl.DataFrame:
    """Find players with EO above threshold that are NOT in my team.

    Works in both template and cohort mode. ``effective_ownership`` prefers
    the top-5k slice when one exists (the report asks what the top managers
    own), falling back to broad/global otherwise. Note the EO scale differs
    by mode — percent in template mode, fractions in cohort mode — so pass a
    threshold matching the mode (e.g. 5.0 for template, 0.05 for cohort).

    Rows carry price, expected points, news, and the next three fixtures
    (opponent short name + home/away), sorted by EO descending.
    """
    eo = compute_effective_ownership(storage, gw)
    if eo.is_empty():
        return _empty(GAP_SCHEMA)

    candidates = (
        eo.with_columns(pl.coalesce("eo_top5k", "eo_broad").alias("effective_ownership"))
        .filter(
            (pl.col("effective_ownership") >= eo_threshold)
            & ~pl.col("fpl_id").is_in(storage.get_my_picks(gw))
        )
        .select("fpl_id", "web_name", "team_name", "position", "effective_ownership", "source")
    )
    if candidates.is_empty():
        return _empty(GAP_SCHEMA)

    gap = (
        candidates.join(
            storage.get_players().select("fpl_id", "now_cost", "news", "team"),
            on="fpl_id",
            how="left",
        )
        .join(
            storage.get_player_gw_stats(gw).select("fpl_id", "ep_next"),
            on="fpl_id",
            how="left",
        )
        .join(
            _next_fixtures(storage.get_fixtures_from_gw(gw), storage.get_teams()),
            on="team",
            how="left",
        )
        .sort("effective_ownership", descending=True)
    )
    return _conform(gap, GAP_SCHEMA)


def compute_transfer_flow(
    storage: Storage,
    gw: int,
    top_n: int = 5,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Compute the most popular transfers in and out of cohort squads this GW.

    Returns ``(top_in_df, top_out_df)``, each with transfer counts, the count
    as a percentage of cohort size, and the player's current EO — so a
    heavily-bought player already at high ownership reads as a bandwagon,
    while one still at low ownership reads as an early-mover signal.

    Template-only GWs have no cohort, hence no transfers: both frames empty.
    """
    if is_template_only(gw):
        return _empty(FLOW_SCHEMA), _empty(FLOW_SCHEMA)

    transfers = storage.get_cohort_transfers(gw)
    cohort_size = storage.get_cohort_managers(gw).height
    if transfers.is_empty() or cohort_size == 0:
        return _empty(FLOW_SCHEMA), _empty(FLOW_SCHEMA)

    players = storage.get_players()
    meta = _player_meta(players).join(players.select("fpl_id", "now_cost"), on="fpl_id", how="left")
    current_eo = compute_effective_ownership(storage, gw).select(
        "fpl_id", pl.coalesce("eo_top5k", "eo_broad").fill_null(0.0).alias("current_eo")
    )

    def _top(direction_col: str) -> pl.DataFrame:
        flow = (
            _flow_counts(transfers, direction_col, cohort_size)
            .join(meta, on="fpl_id", how="left")
            .join(current_eo, on="fpl_id", how="left")
            .with_columns(pl.col("current_eo").fill_null(0.0))
            .sort("transfer_count", descending=True)
            .head(top_n)
        )
        return _conform(flow, FLOW_SCHEMA)

    return _top("fpl_id_in"), _top("fpl_id_out")
