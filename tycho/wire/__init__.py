"""Integration with things outside this process: the harness hooks, the commit
trailer, the status bar, the PyPI update check.

Kept empty on purpose: `test_invariants.TRUST_PATH` scans a package wholesale when its name
resolves to an `__init__.py`, so listing modules there must stay a deliberate, per-module
choice.
"""
