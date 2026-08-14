"""Shared CLI plumbing for the ingest jobs."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence


def parse_args(description: str, argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch as normal but log the R2 writes instead of performing them",
    )
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
