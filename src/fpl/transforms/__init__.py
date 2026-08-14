"""DuckDB transforms shared by the live ingest and the historical backfill.

Every function takes a DuckDB connection and writes local files, leaving the
bucket to the jobs. Work that both pipelines must perform identically lives
here, so that deriving `fact_team_fixture`, projecting through the column
contract and extending the master tables each have one implementation.
"""
