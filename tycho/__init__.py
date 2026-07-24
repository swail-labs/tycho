"""Tycho — a local verifier that fires when an agent claims it's done.

Proves the agent's work from what lives on the developer's machine: git, the filesystem,
process exit codes, and the harness event stream. Only code renders a verdict — no LLM in the
trust path. Free, open source (Apache 2.0), offline, no account needed.
"""

# Alpha. Bumped to 0.1.0 for the first public release (TYCHO-10).
__version__ = "0.1.0"
