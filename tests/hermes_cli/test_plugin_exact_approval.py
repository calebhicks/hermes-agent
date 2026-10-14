"""One-use plugin approval reaches the native gate."""

def test_always_prompt_reaches_native_gate(monkeypatch):
    from hermes_cli.plugins import resolve_pre_tool_block
    seen = {}
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: [{
            "action": "approve", "always_prompt": True,
            "rule_key": "one-call",
        }],
    )
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda *args, **kwargs: seen.update(kwargs) or {"approved": True},
    )
    assert resolve_pre_tool_block("nb_canada_execute", {}) is None
    assert seen["always_prompt"] is True
