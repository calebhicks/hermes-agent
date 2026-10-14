"""Legacy scaffold settings cannot hide native skill discovery."""

import json

from agent.prompt_builder import build_skills_system_prompt


def test_native_discovery_lists_leaf_even_with_stale_scaffold_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps({"skills": {"index_mode": "scaffold"}}))
    leaf = tmp_path / "skills" / "review-example"
    leaf.mkdir(parents=True)
    (leaf / "SKILL.md").write_text(
        "---\nname: review-example\ndescription: Review an example.\n---\nDo the review.\n"
    )
    prompt = build_skills_system_prompt(
        available_tools={"skills_list", "skill_view"}, skills_dir_override=tmp_path / "skills"
    )
    assert "review-example" in prompt
    assert "Review an example." in prompt
    assert "general" in prompt
    assert "available_skill_families" not in prompt
