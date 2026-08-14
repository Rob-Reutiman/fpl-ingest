"""One-shot historical backfill of past seasons from the community archive.

Separate from the live ingest jobs: it runs on `workflow_dispatch`, reads a
third-party GitHub repo rather than the FPL API, and writes curated Parquet.
"""
