"""Instance reclamation is implemented inline in :mod:`behave_rv.engine.loop`
(`_retire_entity` for terminal events, `_reclaim_quiescent` for the quiescence
TTL). This module is kept only so the package layout matches the project
design; it intentionally contains no code."""
