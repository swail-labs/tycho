"""Everything that reads the outside world: the harness transcript, git, the
filesystem. `session.gather()` is the boundary — downstream of it is a frozen `Session` and
pure code.

Kept empty on purpose: `test_invariants.TRUST_PATH` scans a package wholesale when its name
resolves to an `__init__.py`, so listing modules there must stay a deliberate, per-module
choice.
"""
