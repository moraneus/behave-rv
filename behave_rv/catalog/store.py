"""Load and save the catalog artifact (e.g. catalog.json), versioned and committed to the repo.

The catalog is the interface contract between the agent's code and the human's
policies. It is written as stable, diffable JSON so a signature change to a used
step shows up as a reviewable line in version control.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from os import PathLike

from behave_rv.catalog.entry import CatalogEntry

# v2 (2026-07-12): signatures gained helper_hashes and unresolved_calls, and
# the fingerprint algorithm covers the reachable call graph
# (see STABILITY.md). v1 catalogs are refused with a
# recompute message rather than producing spurious breaks.
CATALOG_FORMAT_VERSION = 2


def save_catalog(path: str | PathLike[str], entries: Iterable[CatalogEntry]) -> None:
    document = {
        "catalog_format_version": CATALOG_FORMAT_VERSION,
        "entries": [entry.to_dict() for entry in entries],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_catalog(path: str | PathLike[str]) -> list[CatalogEntry]:
    with open(path, encoding="utf-8") as fh:
        document = json.load(fh)
    version = document.get("catalog_format_version")
    if version != CATALOG_FORMAT_VERSION:
        raise ValueError(
            f"catalog {path} is format v{version}; this tool writes "
            f"v{CATALOG_FORMAT_VERSION} (the fingerprint now covers the call "
            "graph). Recompute it with 'python -m behave_rv catalog save' and "
            "commit the regenerated file -- old and new fingerprints are not "
            "comparable, so diffing them would report spurious breaks."
        )
    return [CatalogEntry.from_dict(item) for item in document["entries"]]
