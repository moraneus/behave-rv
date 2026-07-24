# Release checklist

Every release follows these steps, in order. The recurring failure this
list exists to prevent: a claim about engine behavior going stale in a
document that agents or users read as current.

1. **Code green.** `ruff check .` clean; `pytest tests/ demo/ -q` green
   (the CI invocation, not the bare default, which skips `demo/`).
2. **Experiments re-run** if the analyzer, engine semantics, or CLI output
   changed: the relevant `experiments/run_*.sh` scripts, with regenerated
   artifacts committed. The mutation campaign must stay at 0 misses and
   0 unsound scopings; any expectation change is an intended-behavior
   change to state in the commit.
3. **Documentation sweep - all of it.** Update `docs/*.md` for every
   behavioral change, then copy the shipped set into `behave_rv/docs/`
   (the byte-identity test enforces the sync, but only for the seven
   shipped files - README, demo text, and examples are on this checklist,
   not on the test).
4. **Version bump** in `pyproject.toml` and `behave_rv/__init__.py`
   (the smoke test enforces agreement). Final commit titled
   `Release vX.Y.Z`; annotated tag `vX.Y.Z`; GitHub release with notes;
   the release event triggers the PyPI publish workflow.
5. **Verify from PyPI**: fresh venv, `pip install behave-rv==X.Y.Z`,
   exercise the release's headline changes and `python -m behave_rv docs`.
6. **Sweep the companion skill repo** (`rv-monitoring-skill`): its CI
   re-validates mechanics against the new release automatically, but CI
   cannot catch stale PROSE - grep every `.md` there (SKILL.md,
   references/, docs/, demo narratives) for claims about engine behavior
   this release changed, and update them. A version-behind sentence in a
   skill document becomes an agent's confident false belief.
7. **Paper notes**: if measured numbers or mechanisms changed, append a
   section to the writer's notes file describing exactly which figures
   and claims moved.
