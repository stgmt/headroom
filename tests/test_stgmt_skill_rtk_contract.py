"""Regression contract for the fork-owned RTK maintenance guidance."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "headroom-sub2api-maintainer" / "SKILL.md"
GOTCHAS = ROOT / "docs" / "stgmt-gotchas.md"


def test_skill_selects_rtk_installer_by_actual_claude_host() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "same OS/user account that executes Claude Code Bash" in text
    assert "install-claude-rtk.ps1" in text
    assert "install-claude-rtk.sh" in text
    assert "Native Ubuntu/Hyper-V Claude" in text
    assert "Never install RTK only in a devcontainer" in text


def test_skill_requires_accuracy_exclusions_and_live_claude_proof() -> None:
    text = SKILL.read_text(encoding="utf-8")
    flat = " ".join(text.split())

    for command in ("cat", "git diff", "git show", "curl"):
        assert command in text
    assert "Hook PreToolUse:Bash ... success" in text
    assert "modified tool input keys" in text
    assert "increment in that Claude host's `rtk gain --format json`" in text
    assert "matching host/container totals only when Headroom mounts the same RTK state" in flat


def test_gotcha_ledger_keeps_native_linux_proof_and_owned_installer() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")

    assert "Native Linux/Hyper-V variant" in text
    assert "scripts/install-claude-rtk.sh" in text
    assert "devcontainer-ubuntu-2404" in text
    assert "51,221 -> 7,792" in text
    assert "history `3 -> 4`" in text
