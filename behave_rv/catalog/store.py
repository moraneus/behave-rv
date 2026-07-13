"""Load and save the catalog artifact (e.g. catalog.json), versioned and committed to the repo.

The catalog is the interface contract between the agent's code and the human's
policies. It is written as stable, diffable JSON so a signature change to a used
step shows up as a reviewable line in version control.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from os import PathLike
from typing import Optional

from behave_rv.catalog.app_surface import EmitSite
from behave_rv.catalog.entry import CatalogEntry

# v2 (2026-07-12): signatures gained helper_hashes and unresolved_calls, and
# the fingerprint algorithm covers the reachable call graph (see docs/STABILITY.md).
# v3 (2026-07-13): an optional app_surface section records the application's
# fingerprinted emit sites; step entries are unchanged, so v2 catalogs remain
# readable (their app surface is simply absent). v1 catalogs are refused with
# a recompute message rather than producing spurious breaks.
CATALOG_FORMAT_VERSION = 3
_READABLE_VERSIONS = (2, 3)


def save_catalog(
    path: str | PathLike[str],
    entries: Iterable[CatalogEntry],
    app_surface: Optional[Iterable[EmitSite]] = None,
) -> None:
    document = {
        "catalog_format_version": CATALOG_FORMAT_VERSION,
        "entries": [entry.to_dict() for entry in entries],
    }
    if app_surface is not None:
        document["app_surface"] = [site.to_dict() for site in app_surface]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _load_document(path: str | PathLike[str]) -> dict:
    with open(path, encoding="utf-8") as fh:
        document = json.load(fh)
    version = document.get("catalog_format_version")
    if version not in _READABLE_VERSIONS:
        raise ValueError(
            f"catalog {path} is format v{version}; this tool writes "
            f"v{CATALOG_FORMAT_VERSION} (the fingerprint now covers the call "
            "graph). Recompute it with 'python -m behave_rv catalog save' and "
            "commit the regenerated file -- old and new fingerprints are not "
            "comparable, so diffing them would report spurious breaks."
        )
    return document


def load_catalog(path: str | PathLike[str]) -> list[CatalogEntry]:
    return [CatalogEntry.from_dict(item) for item in _load_document(path)["entries"]]


def load_app_surface(path: str | PathLike[str]) -> Optional[list[EmitSite]]:
    """The committed emit sites, or ``None`` when the catalog predates the app
    surface (never saved with ``--app``) -- callers must treat that as "check
    not enabled", which is different from an empty surface."""
    document = _load_document(path)
    if "app_surface" not in document:
        return None
    return [EmitSite.from_dict(item) for item in document["app_surface"]]
