"""DuckDB transforms shared by the live ingest and the historical backfill.

Pure logic: every function takes a DuckDB connection and writes local files. The
jobs own the bucket. Anything both pipelines must do identically — deriving
`fact_team_fixture`, projecting through the column contract, extending the master
tables — belongs here rather than in one pipeline with the other reimplementing it.
"""
