"""`tool_call_provenance` — did the agent's prose claim a tool action that never happened?

Advisory: it reports, it never sinks a run. The input is prose the agent may be quoting, so
a verdict-bearing FAIL here would be attacker-controllable.
"""

from __future__ import annotations

import re

from ..model import CheckResult, CheckStatus, Session
from .common import _r


# tool_call_provenance families: (label, claim pattern, tool-name substrings). Broad by design —
# a claim requires *some* tool call of that family, never a content match. An unclassifiable
# claim is simply not counted.
_PROV_WEB = re.compile(
    r"\b(?:searched (?:the web|online)|web[- ]?searched|googled|"
    r"browsed to|fetched the (?:page|url|web ?site|site))\b",
    re.IGNORECASE,
)
# A ticket anchor near an action cue, in either order. Both cue kinds are past-only, so a
# future "I'll move ACME-123" can't fire: a past-tense verb, or a two-status arrow.


# A ticket anchor near an action cue, in either order. Both cue kinds are past-only, so a
# future "I'll move ACME-123" can't fire: a past-tense verb, or a two-status arrow.
_ISSUE_ANCHOR = r"(?:\b[A-Z][A-Z0-9]+-\d+\b|\b[Jj]ira (?:ticket|issue|card)\b)"


_ISSUE_STATUS = r"(?:To ?Do|In Progress|In Review|Backlog|Blocked|Hold|Done)"


_ISSUE_CUE = (
    r"(?i:\b(?:created|filed|moved|transitioned|assigned|re-?opened|closed|linked|"
    rf"commented on)\b|{_ISSUE_STATUS}\s*(?:→|-+>|↔)\s*{_ISSUE_STATUS})"
)


_PROV_ISSUE = re.compile(
    rf"{_ISSUE_CUE}[^.\n]{{0,40}}?{_ISSUE_ANCHOR}|{_ISSUE_ANCHOR}[^.\n]{{0,40}}?{_ISSUE_CUE}",
)


_PROVENANCE_FAMILIES = (
    ("a web search/fetch", _PROV_WEB, ("search", "fetch", "browse", "web")),
    ("an issue-tracker action", _PROV_ISSUE, ("jira", "atlassian", "issue", "linear")),
)

# A third-party subject or pre-existing state is the agent *narrating* ("Dan closed ACME-123"),


# A third-party subject or pre-existing state is the agent *narrating* ("Dan closed ACME-123"),
# so those clauses are neutralized first; subject-dropped and first-person survive. Recall loss
# is accepted over a false FAIL. Case-sensitive names, so "and moved" isn't read as a subject.
_REPORTED_SUBJECT = (
    r"(?:he|she|they|"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|"                        # a name: "Dan", "Dan Mano"
    r"[Tt]he\s+(?:operator|owner|user|maintainer|reviewer|team)|"
    r"(?:was|were|been|had|has|is|are))"                      # passive / pre-existing state
)


_REPORTED_VERB = (
    r"(?i:created|filed|moved|transitioned|assigned|re-?opened|closed|linked|commented|"
    r"searched|googled|browsed|fetched)"
)


_REPORTED = re.compile(
    rf"\b{_REPORTED_SUBJECT}\s+(?:already |just |then |recently |now )?{_REPORTED_VERB}"
)

# An observed state ("the board shows In Review → Done") reports where a ticket is, not a


# An observed state ("the board shows In Review → Done") reports where a ticket is, not a
# transition the agent made — neutralized through its arrow. A self-claim keeps an action verb.
_STATE_VERB = r"(?i:sits?|stands?|shows?|reads?|remains?|stays?|is|are|'s|was|were)"


_REPORTED_STATE = re.compile(
    rf"(?:{_ISSUE_ANCHOR}|\bit\b|\b[Tt]he\s+(?:ticket|card|board|issue))\s+"
    rf"(?i:already |just |now |currently |still )*{_STATE_VERB}\b"
    # Bounded: unbounded, this re-scans the line from every "it is" — 192 KB cost 80s.
    rf"[^.\n]{{0,80}}?{_ISSUE_STATUS}\s*(?:→|-+>|↔)\s*{_ISSUE_STATUS}"
)


# Spans the agent is *showing*, not asserting. Dropping them removed 9 of 41 matches over this
# machine's real transcripts. Apostrophes are left alone — `39's` would swallow the sentence.
_SHOWN_SPANS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"^\s*>.*$", re.MULTILINE),
    re.compile(r"\"[^\"\n]{0,300}\"|“[^”\n]{0,300}”"),
)


def _claimed_families(session: Session) -> list[tuple[str, tuple[str, ...]]]:
    """(label, tool-substrings) per provenance family claimed in the turn's assistant prose.
    Shared with `has_verifiable_activity` so they can't disagree."""
    prose = "\n".join(m.text for m in session.turn_messages)
    for pattern in _SHOWN_SPANS:
        prose = pattern.sub(" ", prose)
    prose = _REPORTED.sub(" ", prose)        # drop narrated third-party / pre-existing actions
    prose = _REPORTED_STATE.sub(" ", prose)  # drop observed ticket-state (arrow ≠ a self-transition)
    return [(label, tools) for label, pat, tools in _PROVENANCE_FAMILIES if pat.search(prose)]


def tool_call_provenance(session: Session) -> CheckResult:
    """Did the agent's prose claim a tool action that never happened? **Advisory** — it
    reports, it never sinks a run. A claimed family must be backed by *some* tool call of that
    family; no prose, or no recognized claim, is UNSUPPORTED.

    An unbacked claim is UNSUPPORTED, not FAIL, deliberately: the input is prose the agent may
    be *quoting*, so a verdict-bearing FAIL here would be attacker-controllable. The asymmetry
    survives — prose cannot conjure a tool call, so PASS still requires one that happened.

    **A known regression against strategy §7**, which has this check rising in value. The
    narrower first-person rule that would keep the teeth was measured against 6,584 real
    assistant messages: of 41 matches, 4 were first-person governed and one of those was a
    false positive. Agents write tool actions subject-dropped ("Created TYCHOE-11"), so that
    rule keeps ~10% of real claims and none of the web family. Restoring the teeth needs a
    claim whose *author* is known — a declared claim channel, not a better pattern over prose.
    """
    if not session.turn_messages:
        return _r("tool_call_provenance", CheckStatus.UNSUPPORTED, "no assistant prose captured to check")
    claimed = _claimed_families(session)
    if not claimed:
        return _r("tool_call_provenance", CheckStatus.UNSUPPORTED, "no tool-action claims recognized in the turn")
    tools = [e.tool.lower() for e in session.turn_events]
    unbacked = [label for label, subs in claimed if not any(s in t for t in tools for s in subs)]
    if unbacked:
        return _r(
            "tool_call_provenance",
            CheckStatus.UNSUPPORTED,
            f"claimed {', '.join(unbacked)} with no matching tool call this turn — advisory:"
            " prose can be quoted or injected, so this is a hint to confirm, not a verdict",
        )
    backed = ", ".join(label for label, _ in claimed)
    return _r("tool_call_provenance", CheckStatus.PASS, f"claimed actions are backed by tool calls ({backed})")
