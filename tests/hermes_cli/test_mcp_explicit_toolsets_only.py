"""A managed profile's no-global-union boundary survives ordinary config edits."""
from copy import deepcopy

import pytest

from hermes_cli.config import load_config, save_config
from hermes_cli.tools_config import _get_platform_tools, _save_platform_tools


def test_explicit_only_survives_real_save_reload_and_server_loss():
    for lane in ("cli", "imessage", "a2a", "cron", "kanban", "slack"):
        for allowed in ([], ["mcp-other"]):
            config = {
                "mcp": {"explicit_toolsets_only": True},
                "mcp_servers": {
                    "other": {"command": "unused"},
                    "conductor-cloud": {"command": "unused"},
                },
                "platform_toolsets": {lane: ["file", *allowed, "no_mcp"]},
            }
            save_config(config)
            config = load_config()
            _save_platform_tools(config, lane, {"file", "memory"})
            config = load_config()
            assert "no_mcp" not in config["platform_toolsets"][lane]
            for remove in (False, True):
                variant = deepcopy(config)
                if remove:
                    variant["mcp_servers"].pop("other")
                else:
                    variant["mcp_servers"]["other"]["enabled"] = False
                actual = _get_platform_tools(variant, lane)
                assert not {"conductor-cloud", "mcp-conductor-cloud"} & actual
                assert set(allowed) <= actual
            owner = deepcopy(config)
            owner["platform_toolsets"][lane] = ["file", "conductor-cloud"]
            assert "conductor-cloud" in _get_platform_tools(owner, lane)
    legacy = {"mcp_servers": {"other": {"command": "unused"}}, "platform_toolsets": {"cli": ["file"]}}
    assert "other" in _get_platform_tools(legacy, "cli")


@pytest.mark.parametrize("value", ["false", None, [], {}, 0, 1])
def test_malformed_explicit_only_is_rejected(value):
    with pytest.raises(ValueError, match="must be a boolean"):
        _get_platform_tools({"mcp": {"explicit_toolsets_only": value}}, "cli")
