"""`<repo>/.tycho/`: what init wired, the turn record, the exec log, the config.
Every writer here is a concurrent writer, and none of them may raise into a caller.

Kept empty on purpose: `test_invariants.TRUST_PATH` scans a package wholesale when its name
resolves to an `__init__.py`, so listing modules there must stay a deliberate, per-module
choice.
"""
