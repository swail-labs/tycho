"""Scope management: `.tycho.toml` create-on-init, `tycho scope` edits, and the
`/tycho-scope-*` slash commands. Zero-config stays intact: an empty include leaves
scope_drift UNSUPPORTED, exactly as with no file at all."""

from pathlib import Path

from tycho.engine import checks
from tycho import cli
from tycho.store import config as config_mod
from tycho.wire import install as init_mod
from tycho.model import FileEdit, GitSnapshot, Session

CLAUDE = Path(".claude/settings.json")


def _session(repo: Path, edits) -> Session:
    return Session(
        events=(),
        edits=tuple(edits),
        repo=repo,
        config=config_mod.load(repo),
        files={},
        git=GitSnapshot(True, "abc", ()),
    )


# --- config: read/write round-trips -----------------------------------------

def test_add_dedups_and_preserves_order(tmp_path: Path):
    assert config_mod.add_scope(tmp_path, ["src/**", "tests/**"]) == ("src/**", "tests/**")
    assert config_mod.add_scope(tmp_path, ["src/**", "docs/**"]) == ("src/**", "tests/**", "docs/**")


def test_set_replaces_wholesale_and_remove_drops(tmp_path: Path):
    config_mod.add_scope(tmp_path, ["a/**", "b/**"])
    assert config_mod.set_scope(tmp_path, ["x", "y", "z"]) == ("x", "y", "z")
    assert config_mod.remove_scope(tmp_path, ["y"]) == ("x", "z")


def test_multiple_globs_in_one_call(tmp_path: Path):
    assert config_mod.set_scope(tmp_path, ["src/**", "tests/**", "docs/**"]) == ("src/**", "tests/**", "docs/**")


def test_scope_edits_preserve_disabled_checks(tmp_path: Path):
    config_mod.path(tmp_path).write_text(config_mod.render(("src/**",), ("command_execution",)), encoding="utf-8")
    config_mod.add_scope(tmp_path, ["tests/**"])
    cfg = config_mod.load(tmp_path)
    assert cfg.scope_include == ("src/**", "tests/**")
    assert cfg.disabled_checks == ("command_execution",)  # not wiped by a scope edit


def test_written_file_round_trips_through_tomllib(tmp_path: Path):
    config_mod.set_scope(tmp_path, ['weird "quote"/**', "a\\b/**"])  # chars that need escaping
    assert config_mod.load(tmp_path).scope_include == ('weird "quote"/**', "a\\b/**")


def test_standing_filters_load_from_a_hand_written_file(tmp_path: Path):
    """Hand-editing is the documented way in, so the key has to parse from a file nothing in
    Tycho wrote."""
    config_mod.path(tmp_path).write_text(
        '[tests]\nstanding = ["-m \\"not e2e\\"", "--ignore docs"]\n', encoding="utf-8"
    )
    assert config_mod.load(tmp_path).standing_filters == ('-m "not e2e"', "--ignore docs")


def test_standing_filters_survive_a_scope_edit(tmp_path: Path):
    """Every setter re-renders the whole file, so a key missing from the template is silently
    dropped by an unrelated edit."""
    config_mod.path(tmp_path).write_text(
        '[tests]\nstanding = ["-m \\"not e2e\\""]\n', encoding="utf-8"
    )
    config_mod.add_scope(tmp_path, ["src/**"])
    assert config_mod.load(tmp_path).standing_filters == ('-m "not e2e"',)


def test_zero_config_declares_no_standing_filters(tmp_path: Path):
    assert config_mod.load(tmp_path).standing_filters == ()
    config_mod.ensure(tmp_path)
    assert config_mod.load(tmp_path).standing_filters == ()


# --- ensure: create on init, never clobber ----------------------------------

def test_ensure_creates_then_is_a_noop(tmp_path: Path):
    assert config_mod.ensure(tmp_path) is True
    assert config_mod.path(tmp_path).exists()
    assert config_mod.load(tmp_path).scope_include == ()  # empty → zero-config behaviour
    assert config_mod.ensure(tmp_path) is False  # second run doesn't touch it


def test_ensure_never_clobbers_existing(tmp_path: Path):
    config_mod.path(tmp_path).write_text('[scope]\ninclude = ["mine/**"]\n', encoding="utf-8")
    assert config_mod.ensure(tmp_path) is False
    assert config_mod.load(tmp_path).scope_include == ("mine/**",)


def test_init_creates_tycho_toml(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)
    assert config_mod.path(tmp_path).exists()
    assert any(config_mod.CONFIG_NAME in line for line in lines)


# --- end to end: the written file drives scope_drift ------------------------

def test_scope_drift_reads_the_written_file(tmp_path: Path):
    config_mod.set_scope(tmp_path, ["src/**"])
    inside = _session(tmp_path, [FileEdit("src/app.py", 1.0, None, "edit")])
    outside = _session(tmp_path, [FileEdit("infra/deploy.sh", 1.0, None, "edit")])
    assert checks.scope_drift(inside).status.name == "PASS"
    r = checks.scope_drift(outside)
    assert r.status.name == "FAIL" and "infra/deploy.sh" in r.evidence


def test_zero_config_leaves_scope_drift_unsupported(tmp_path: Path):
    # No .tycho.toml at all → UNSUPPORTED, unchanged from today.
    s = _session(tmp_path, [FileEdit("anything.py", 1.0, None, "edit")])
    assert checks.scope_drift(s).status.name == "UNSUPPORTED"


# --- the four /tycho-scope-* slash commands ---------------------------------

def test_init_generates_four_scope_slash_commands_with_hints(tmp_path: Path):
    files = {p.name: body for p, body in init_mod._slash_files(tmp_path).items()}
    for action in ("list", "set", "add", "remove"):
        assert f"tycho-scope-{action}.md" in files
    # set/add/remove take globs: they must carry an argument-hint and pass $ARGUMENTS through,
    # and tell the agent to quote each glob so the shell doesn't expand it.
    for action in ("set", "add", "remove"):
        body = files[f"tycho-scope-{action}.md"]
        assert "argument-hint:" in body
        assert "$ARGUMENTS" in body
        assert "single-quoted" in body
        assert f"scope {action}" in body
    assert "argument-hint:" not in files["tycho-scope-list.md"]  # list takes no args


# --- CLI ---------------------------------------------------------------------

def test_cli_scope_add_list_remove(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scope", "add", "src/**", "tests/**"]) == cli.ExitCode.OK
    assert config_mod.load(tmp_path).scope_include == ("src/**", "tests/**")

    assert cli.main(["scope", "list"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "src/**" in out and "tests/**" in out

    cli.main(["scope", "remove", "tests/**"])
    assert config_mod.load(tmp_path).scope_include == ("src/**",)


def test_cli_scope_add_without_globs_is_a_usage_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scope", "add"]) == cli.ExitCode.USAGE


def test_cli_scope_list_when_empty_explains_zero_config(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["scope", "list"])
    assert "none set" in capsys.readouterr().out


# --- exclude / denylist ------------------------------------------

def test_exclude_edits_round_trip_and_preserve_include(tmp_path: Path):
    config_mod.set_scope(tmp_path, ["**"])
    assert config_mod.add_exclude(tmp_path, ["LICENSE", "secrets/**"]) == ("LICENSE", "secrets/**")
    cfg = config_mod.load(tmp_path)
    assert cfg.scope_include == ("**",)  # untouched by the exclude edit
    assert cfg.scope_exclude == ("LICENSE", "secrets/**")
    assert config_mod.remove_exclude(tmp_path, ["secrets/**"]) == ("LICENSE",)
    assert config_mod.load(tmp_path).scope_include == ("**",)  # still untouched


def test_exclude_wins_over_include_in_scope_drift(tmp_path: Path):
    config_mod.set_scope(tmp_path, ["**"])          # whole tree allowed…
    config_mod.set_exclude(tmp_path, ["LICENSE"])   # …except this one file
    ok = _session(tmp_path, [FileEdit("tycho/cli.py", 1.0, None, "edit")])
    bad = _session(tmp_path, [FileEdit("LICENSE", 1.0, None, "edit")])
    assert checks.scope_drift(ok).status.name == "PASS"
    r = checks.scope_drift(bad)
    assert r.status.name == "FAIL" and "LICENSE" in r.evidence


def test_empty_exclude_is_a_pure_allowlist(tmp_path: Path):
    config_mod.set_scope(tmp_path, ["src/**"])  # no exclude → identical to older
    s = _session(tmp_path, [FileEdit("src/app.py", 1.0, None, "edit")])
    assert checks.scope_drift(s).status.name == "PASS"


def test_exclude_without_include_stays_unsupported(tmp_path: Path):
    # exclude only matters relative to an include; with none, scope_drift is still zero-config.
    config_mod.set_exclude(tmp_path, ["LICENSE"])
    s = _session(tmp_path, [FileEdit("LICENSE", 1.0, None, "edit")])
    assert checks.scope_drift(s).status.name == "UNSUPPORTED"


def test_cli_scope_add_exclude_and_list_shows_both(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["scope", "set", "**"])
    assert cli.main(["scope", "add", "--exclude", "LICENSE"]) == cli.ExitCode.OK
    assert config_mod.load(tmp_path).scope_exclude == ("LICENSE",)
    cli.main(["scope", "list"])
    out = capsys.readouterr().out
    assert "**" in out and "LICENSE" in out and "except" in out
