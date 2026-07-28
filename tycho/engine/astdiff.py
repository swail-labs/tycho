"""Compare two versions of a Python source file for tell-tale test-gaming edits.

Stdlib `ast` only. Every entry point returns evidence strings (empty = nothing
found), and returns `[]` when either side won't parse — an unparseable file is
UNSUPPORTED, never a false FAIL.
"""

from __future__ import annotations

import ast

_MOCK_NAMES = frozenset({"patch", "mock", "Mock", "MagicMock", "AsyncMock"})


def assertion_delta(before: str | None, after: str | None) -> list[str]:
    """Assertions removed or neutralized to always-true between before and after."""
    b, a = _parse(before), _parse(after)
    if b is None or a is None:
        return []
    findings = []
    removed = _count_asserts(b) - _count_asserts(a)
    if removed > 0:
        findings.append(f"{removed} assertion(s) removed")
    neutralized = _count_neutralized(a) - _count_neutralized(b)
    if neutralized > 0:
        findings.append(f"{neutralized} assertion(s) neutralized to always-true")
    return findings


def skip_or_mock_added(before: str | None, after: str | None) -> list[str]:
    """Skip decorators or mock/patch uses newly introduced in after."""
    b, a = _parse(before), _parse(after)
    if b is None or a is None:
        return []
    findings = []
    skips_before, skips_after = _skip_decorators(b), _skip_decorators(a)
    if len(skips_after) > len(skips_before):
        added = sorted(set(skips_after) - set(skips_before)) or skips_after
        findings.append(f"skip added: {', '.join(added)}")
    mocks_added = _mock_uses(a) - _mock_uses(b)
    if mocks_added > 0:
        findings.append(f"{mocks_added} mock/patch use(s) added")
    return findings


def parseable(src: str | None) -> bool:
    """Whether these differs can actually read this file — Python only.

    Callers must ask *before* reading an empty finding list as an all-clear. `[]` means "found
    nothing", and for a `.test.js` file it means "found nothing because I cannot read
    JavaScript" — reporting that as PASS is a fabricated green, the one verdict this codebase
    never issues.
    """
    return _parse(src) is not None


def _parse(src: str | None) -> ast.AST | None:
    if not src:
        return None
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _count_asserts(tree: ast.AST) -> int:
    """`assert` statements plus unittest-style `self.assert*(...)` calls."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.Call) and _dotted(node.func).rsplit(".", 1)[-1].startswith("assert"):
            count += 1
    return count


def _count_neutralized(tree: ast.AST) -> int:
    """`assert True` / `assert 1` — asserts that can never fail."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
        and isinstance(node.test, ast.Constant)
        and bool(node.test.value)
    )


def _skip_decorators(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.extend(name for d in node.decorator_list if "skip" in (name := _dotted(d)).lower())
    return out


def _mock_uses(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _MOCK_NAMES:
            count += 1
        elif isinstance(node, ast.Attribute) and node.attr in _MOCK_NAMES:
            count += 1
    return count
