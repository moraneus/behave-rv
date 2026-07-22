"""behave-rv: runtime verification over a live event stream, authored in Gherkin.

Built ON `behave`, not a fork of it: the Gherkin parser and model are reused
unchanged as an ordinary dependency (``behave_rv.vendor_behave`` marks the
boundary), and everything downstream -- the deterministic per-entity engine,
the temporal vocabulary, and the two-sided stability contract -- is new.
"""

__version__ = "0.1.0"
