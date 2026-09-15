"""Exact MCP selectors constrain discovery, progressive disclosure, and dispatch."""
import json
from unittest.mock import Mock


def test_spawn_filter_keeps_exact_servers_only():
    from hermes_cli.mcp_startup import set_mcp_server_filter
    assert set_mcp_server_filter("ceo-brain:search,ceo-brain:get_page,skills_read") == ["ceo-brain", "skills_read"]
    assert set_mcp_server_filter(None) is None


def test_exact_selection_blocks_guessed_direct_and_deferred_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import model_tools
    from tools.registry import registry
    calls = []
    for op in ("search", "put_page"):
        name = f"mcp__fixture__{op}"
        registry.register(name=name, toolset="mcp-fixture", handler=lambda args, **kw: calls.append(args) or '{"result": []}',
                          schema={"name": name, "description": op, "parameters": {"type": "object", "properties": {}}})
    registry.register_toolset_alias("fixture", "mcp-fixture")
    try:
        selected = ["fixture:search", "skills_read"]
        defs = model_tools.get_tool_definitions(selected, quiet_mode=True, skip_tool_search_assembly=True)
        names = {d["function"]["name"] for d in defs}
        assert "mcp__fixture__search" in names
        assert "mcp__fixture__put_page" not in names
        assert "skill_manage" not in names
        assert {"skills_list", "skill_view"} <= names
        result = model_tools.handle_function_call("mcp__fixture__put_page", {}, enabled_toolsets=selected)
        assert "error" in json.loads(result)
        assert calls == []
        result, _ = model_tools._dispatch_bridge_tool("tool_describe", {"names": ["mcp__fixture__put_page"]}, selected, None)
        assert "mcp__fixture__put_page" not in {d["function"]["name"] for d in model_tools.get_tool_definitions(selected, quiet_mode=True, skip_tool_search_assembly=True)}
        model_tools.handle_function_call("mcp__fixture__search", {}, enabled_toolsets=selected)
        assert len(calls) == 1
    finally:
        for op in ("search", "put_page"):
            registry.deregister(f"mcp__fixture__{op}")


def test_readonly_skill_view_does_not_execute_template(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills = tmp_path / "skills" / "fixture"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text('---\nname: fixture\ndescription: fixture instructions\n---\nPlain guidance !`echo NEVER_EXECUTE`\n')
    import model_tools
    import tools.skills_tool as st
    sentinel = Mock(side_effect=AssertionError("Template execution forbidden"))
    monkeypatch.setattr(st, "_preprocess_skill", sentinel)
    result = model_tools.handle_function_call("skill_view", {"name": "fixture"}, enabled_toolsets=["skills_read"])
    assert "Plain guidance" in result
    sentinel.assert_not_called()
    escaped = model_tools.handle_function_call("skill_view", {"name": "../config.yaml"}, enabled_toolsets=["skills_read"])
    assert "Plain guidance" not in escaped
