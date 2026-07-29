"""Tycho — a local verifier that fires when an agent claims it's done.

Proves the agent's work from what lives on the developer's machine: git, the filesystem,
process exit codes, and the harness event stream. Only code renders a verdict — no LLM in the
trust path. Free, open source (Apache 2.0), offline, no account needed.
"""

# The single source of truth for the version: Hatchling reads it, the release workflow asserts
# the tag matches it, and the npm wrapper and Homebrew formula are stamped from that tag.
__version__ = "0.2.0"
