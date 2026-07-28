"""`tycho uninstall`: remove only Tycho-owned hooks, idempotently."""

import json
from pathlib import Path

from tycho import cli
from tycho import install as init_mod

CLAUDE = Path(".claude/settings.json")
CURSOR = Path(".cursor/hooks.json")
CODEX = Path(".codex/hooks.json")
PLUGIN = Path(".opencode/plugins/tycho.js")


def _read(repo: Path, rel: Path) -> dict:
    return json.loads((repo / rel).read_text())


def _tycho_commands(repo: Path) -> list[str]:
    """Every tycho hook command currently installed across all harnesses."""
    found = []
    for rel, key in ((CLAUDE, "Stop"), (CODEX, "Stop")):
        for group in _read(repo, rel).get("hooks", {}).get(key, []):
            found += [
                h["command"]
                for h in group.get("hooks", [])
                if init_mod._is_tycho_hook(h.get("command"))
            ]
    for entry in _read(repo, CURSOR).get("hooks", {}).get("stop", []):
        if init_mod._is_tycho_hook(entry.get("command")):
            found.append(entry["command"])
    return found


def _init_all(repo: Path, **kw) -> list[str]:
    """Install every harness directly. Auto-detect surfaces only Claude now (Claude-only
    usage), so the non-Claude installers — kept and still exercised here — are called by name."""
    for name in (".claude", ".cursor", ".codex", ".opencode"):
        (repo / name).mkdir(exist_ok=True)
    return [
        init_mod._install_claude(repo),
        init_mod._install_cursor(repo),
        init_mod._install_codex(repo),
        init_mod._install_opencode(repo),
    ]


# --- the round trip ----------------------------------------------------------

def test_uninstall_removes_every_tycho_hook(tmp_path: Path):
    _init_all(tmp_path)
    assert _tycho_commands(tmp_path), "precondition: init installed hooks"
    init_mod.uninstall(tmp_path)
    assert _tycho_commands(tmp_path) == []
    assert not (tmp_path / PLUGIN).exists()


def test_install_reinstall_uninstall_reinstall_cycle(tmp_path: Path):
    _init_all(tmp_path)
    _init_all(tmp_path)  # reinstall must not duplicate
    assert len(_tycho_commands(tmp_path)) == 3  # claude + cursor + codex
    init_mod.uninstall(tmp_path)
    assert _tycho_commands(tmp_path) == []
    _init_all(tmp_path)  # must come back cleanly
    assert len(_tycho_commands(tmp_path)) == 3
    assert (tmp_path / PLUGIN).exists()


def test_uninstall_is_idempotent(tmp_path: Path):
    _init_all(tmp_path)
    init_mod.uninstall(tmp_path)
    lines = init_mod.uninstall(tmp_path)  # second pass: nothing left to do
    assert all("nothing to remove" in line for line in lines)


def test_uninstall_on_clean_repo_is_a_noop(tmp_path: Path):
    lines = init_mod.uninstall(tmp_path)  # never initialized — must not raise
    assert all("nothing to remove" in line for line in lines)


# --- don't touch what isn't ours --------------------------------------------

def test_uninstall_preserves_unrelated_settings_and_hooks(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "make lint"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        },
    }))
    _init_all(tmp_path)
    init_mod.uninstall(tmp_path)

    data = _read(tmp_path, CLAUDE)
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"] == [{"hooks": [{"type": "command", "command": "make lint"}]}]
    # the user's own Stop hook survives; only ours is gone
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "notify-send done"}]}]


def test_uninstall_drops_the_stop_list_it_created(tmp_path: Path):
    # We created hooks.Stop, so removing our entry should leave no empty husk.
    _init_all(tmp_path)
    init_mod.uninstall(tmp_path)
    assert "Stop" not in _read(tmp_path, CLAUDE).get("hooks", {})


def test_uninstall_leaves_a_handwritten_opencode_plugin_alone(tmp_path: Path):
    plugin = tmp_path / PLUGIN
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// my own plugin\nexport const Mine = () => ({})\n")
    line = next(line for line in init_mod.uninstall(tmp_path) if line.startswith("opencode"))
    assert "left" in line and plugin.exists()


def test_uninstall_keeps_plugins_dir_when_other_plugins_remain(tmp_path: Path):
    _init_all(tmp_path)
    other = tmp_path / ".opencode" / "plugins" / "other.js"
    other.write_text("export const Other = () => ({})\n")
    init_mod.uninstall(tmp_path)
    assert not (tmp_path / PLUGIN).exists()
    assert other.exists()  # dir must survive because it isn't empty


# --- --harness filter --------------------------------------------------------

def test_uninstall_harness_filter_touches_only_that_harness(tmp_path: Path):
    _init_all(tmp_path)
    init_mod.uninstall(tmp_path, only="claude")
    assert _read(tmp_path, CLAUDE).get("hooks", {}).get("Stop") is None
    assert _tycho_commands(tmp_path)  # cursor + codex still installed
    assert (tmp_path / PLUGIN).exists()


def test_cli_uninstall_exits_ok(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _init_all(tmp_path)
    assert cli.main(["uninstall"]) == cli.ExitCode.OK
    assert "removed" in capsys.readouterr().out
    assert _tycho_commands(tmp_path) == []
