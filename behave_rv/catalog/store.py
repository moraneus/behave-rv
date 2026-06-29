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

CATALOG_FORMAT_VERSION = 1


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
    return [CatalogEntry.from_dict(item) for item in document["entries"]]
