"""Adapt behave's Gherkin model into our AST. Wraps the vendored parser; does not edit it.

This is the reuse seam: we call behave's parser and model directly to turn
``.feature`` text into a model, and never touch behave's runner. The front end
is reused; the back end (engine, verdicts) is ours.
"""

from __future__ import annotations

from behave.parser import parse_feature as _behave_parse_feature
from behave.model import Feature


def parse_feature(text: str, *, filename: str | None = None) -> Feature:
    """Parse ``.feature`` text into a behave :class:`Feature` model.

    A thin wrapper over ``behave.parser.parse_feature``. No scenario is executed;
    only the parser and model are used.
    """
    return _behave_parse_feature(text, filename=filename)
