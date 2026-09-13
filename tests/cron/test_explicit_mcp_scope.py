"""Cron must preserve the managed profile's MCP scope on both resolution paths."""
import pytest

from cron.scheduler import _resolve_cron_enabled_toolsets


def test_per_job_mcp_scope_survives_server_loss_and_keeps_explicit_grants():
    for enabled in (False, True):
        cfg = {"mcp": {"explicit_toolsets_only": True}, "mcp_servers": {
            "other": {"command": "unused", "enabled": enabled},
            "conductor-cloud": {"command": "unused"},
        }}
        for values in (["file"], ["file", "other"], ["file", "no_mcp"]):
            actual = _resolve_cron_enabled_toolsets({"enabled_toolsets": values}, cfg)
            assert "conductor-cloud" not in actual
            assert actual == [v for v in values if v != "no_mcp"]
        assert _resolve_cron_enabled_toolsets({"enabled_toolsets": ["file", "notion-work"]}, cfg) == ["file", "notion-work"]
    cfg["mcp"]["explicit_toolsets_only"] = False
    assert "conductor-cloud" in _resolve_cron_enabled_toolsets({"enabled_toolsets": ["file"]}, cfg)


def test_implicit_cron_failure_never_becomes_default_tools_for_managed_profile(monkeypatch):
    import hermes_cli.tools_config as tools_config
    def broken(*args, **kwargs):
        raise ValueError("fixture resolution failure")
    monkeypatch.setattr(tools_config, "_get_platform_tools", broken)
    for value in (True, "false", None, 0):
        with pytest.raises(RuntimeError, match="cannot use defaults"):
            _resolve_cron_enabled_toolsets({}, {"mcp": {"explicit_toolsets_only": value}})
    assert _resolve_cron_enabled_toolsets({}, {}) is None
