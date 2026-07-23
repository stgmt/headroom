from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_headroom_skill_keeps_cross_session_failure_contract() -> None:
    skill = (ROOT / "skills/headroom-sub2api-maintainer/SKILL.md").read_text(
        encoding="utf-8"
    )
    registry = (
        ROOT
        / "skills/headroom-sub2api-maintainer/references/session-failure-registry.md"
    ).read_text(encoding="utf-8")

    assert "references/session-failure-registry.md" in skill
    assert "canonical full registry" in registry
    for incident in (2, 5, 6, 7, 12, 13, 14, 15, 16, 18, 20, 21, 22, 23, 24, 25):
        assert f"`F{incident:02d}`" in registry
