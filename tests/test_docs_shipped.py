"""The documentation shipped inside the package must match docs/ exactly.

behave_rv/docs/*.md are the copies installed with the wheel (read them with
``python -m behave_rv docs <name>``); docs/*.md are the repository originals.
This test keeps the two in byte-identical sync, so editing one without the
other fails CI.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_docs_are_byte_identical_to_the_repository_docs():
    repo = {p.name: p.read_bytes() for p in (ROOT / "docs").glob("*.md")}
    shipped = {p.name: p.read_bytes() for p in (ROOT / "behave_rv/docs").glob("*.md")}
    assert set(repo) == set(shipped), (
        f"doc sets differ: only in docs/: {set(repo) - set(shipped)}, "
        f"only in package: {set(shipped) - set(repo)} -- "
        "copy docs/*.md to behave_rv/docs/ after editing")
    for name, content in repo.items():
        assert shipped[name] == content, f"{name} drifted -- re-copy it"


def test_docs_command_lists_and_prints():
    listing = subprocess.run([sys.executable, "-m", "behave_rv", "docs"],
                             capture_output=True, text=True, cwd=ROOT)
    assert listing.returncode == 0
    assert "guide" in listing.stdout and "operators" in listing.stdout
    guide = subprocess.run([sys.executable, "-m", "behave_rv", "docs", "guide"],
                           capture_output=True, text=True, cwd=ROOT)
    assert guide.returncode == 0
    assert "behave_rv" in guide.stdout and len(guide.stdout) > 10_000
    missing = subprocess.run([sys.executable, "-m", "behave_rv", "docs", "nope"],
                             capture_output=True, text=True, cwd=ROOT)
    assert missing.returncode == 2
    assert "unknown document" in missing.stderr
