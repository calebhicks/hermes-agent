"""Profile-scoped skill index mode reaches each agent instance."""

from __future__ import annotations

import contextlib
import io
from unittest.mock import MagicMock

from hermes_state import SessionDB
from run_agent import AIAgent


def _make_agent(monkeypatch, tmp_path, skills):
    from hermes_cli import config as config_mod

    config = {
        "skills": skills,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: config)
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: config)
    monkeypatch.setattr(AIAgent, "_create_openai_client", lambda *_args, **_kwargs: MagicMock())
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        return AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="skill-index-mode-test",
        )


def test_scaffold_mode_is_honored(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, {"index_mode": "scaffold"})
    assert agent._skill_index_mode == "scaffold"


def test_full_mode_remains_default(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, {})
    assert agent._skill_index_mode == "full"


def test_unknown_or_malformed_mode_falls_back_to_full(monkeypatch, tmp_path):
    unknown = _make_agent(monkeypatch, tmp_path, {"index_mode": "surprise"})
    malformed = _make_agent(monkeypatch, tmp_path, ["scaffold"])
    assert unknown._skill_index_mode == "full"
    assert malformed._skill_index_mode == "full"


def test_scaffold_mode_survives_malformed_creation_nudge(monkeypatch, tmp_path):
    agent = _make_agent(
        monkeypatch,
        tmp_path,
        {"index_mode": "scaffold", "creation_nudge_interval": "not-a-number"},
    )
    assert agent._skill_nudge_interval == 10
    assert agent._skill_index_mode == "scaffold"
