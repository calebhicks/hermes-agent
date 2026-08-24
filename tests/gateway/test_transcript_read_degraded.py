"""A failed transcript read must be distinguishable from an empty transcript.

2026-08-20: state.db FTS corruption made get_messages_as_conversation raise on
every read; load_transcript returned a bare [] and the gateway served hours of
turns as single-turn conversations — a storage failure impersonated a fresh
session. load_transcript now returns DegradedTranscript (a list subclass) so
list-shaped consumers keep working while the live-turn path can tell the model
its history is unavailable rather than absent.
"""

import sqlite3

from gateway.session import DegradedTranscript, SessionStore
from gateway.config import GatewayConfig


def _make_store(tmp_path, monkeypatch):
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def test_failed_read_returns_degraded_transcript(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    sid = "sess-degraded"
    store._db.create_session(session_id=sid, source="test")
    store.append_to_transcript(sid, {"role": "user", "content": "hi", "timestamp": 1.0})

    def _broken_read(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store._db, "get_messages_as_conversation", _broken_read)

    history = store.load_transcript(sid)
    assert isinstance(history, DegradedTranscript)
    assert history == []  # every list-shaped consumer still sees empty
    assert len(history) == 0
    assert "malformed" in history.error


def test_healthy_read_is_a_plain_list(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    sid = "sess-healthy"
    store._db.create_session(session_id=sid, source="test")
    store.append_to_transcript(sid, {"role": "user", "content": "hi", "timestamp": 1.0})
    store.append_to_transcript(sid, {"role": "assistant", "content": "yo", "timestamp": 2.0})

    history = store.load_transcript(sid)
    assert not isinstance(history, DegradedTranscript)
    assert len(history) == 2


def test_genuinely_empty_session_is_not_degraded(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    sid = "sess-empty"
    store._db.create_session(session_id=sid, source="test")

    history = store.load_transcript(sid)
    assert not isinstance(history, DegradedTranscript)
    assert history == []
