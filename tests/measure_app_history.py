"""Replay this repository's own git history through the app-surface analyzer.

For every commit that modified one of the demo/example application files, the
parent and child versions are extracted (``git show``), analyzed, and
classified -- the honest flag-rate measurement quoted in STABILITY.md. Rerun
it any time with:

    python -m tests.measure_app_history

The numbers change as history grows; the method does not.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from behave_rv.catalog.app_surface import analyze_app, classify_app_changes

APP_FILES = [
    "demo/order_service/service.py",
    "demo/session_service/service.py",
    "demo/todo_app/service.py",
    "demo/todo_app/app.py",
    "examples/ticketing/app_service.py",
]

SILENT = ("unchanged", "renamed")


def _show(rev: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{rev}:{path}"],
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def measure() -> list[tuple[str, str, str, list[str]]]:
    rows = []
    for path in APP_FILES:
        log = subprocess.run(["git", "log", "--format=%h %s", "--", path],
                             capture_output=True, text=True).stdout
        for line in log.strip().splitlines():
            sha, subject = line.split(" ", 1)
            old, new = _show(f"{sha}^", path), _show(sha, path)
            if old is None or new is None or old == new:
                continue   # creation commit, or content untouched by the commit
            with tempfile.TemporaryDirectory() as tmp:
                old_dir, new_dir = Path(tmp) / "old", Path(tmp) / "new"
                old_dir.mkdir(), new_dir.mkdir()
                name = Path(path).name
                (old_dir / name).write_text(old)
                (new_dir / name).write_text(new)
                changes = classify_app_changes(analyze_app([old_dir / name]),
                                               analyze_app([new_dir / name]))
            flagged = sorted({c.status for c in changes if c.status not in SILENT})
            rows.append((path, sha, subject, flagged or ["silent"]))
    return rows


def main() -> int:
    rows = measure()
    for path, sha, subject, statuses in rows:
        print(f"{Path(path).name:16} {sha}  {','.join(statuses):22} {subject[:60]}")
    silent = sum(1 for r in rows if r[3] == ["silent"])
    print(f"\n{len(rows)} historical app-file changes: "
          f"{len(rows) - silent} flagged, {silent} silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
