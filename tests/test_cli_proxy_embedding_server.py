"""Regression tests for `proxy --embedding-server` startup behavior."""

import sys

from click.testing import CliRunner

from headroom.cli import main


def test_embedding_server_sidecar_module_is_packaged():
    from headroom.memory.adapters.watchdog import EmbeddingServerWatchdog, SocketEmbedderClient

    assert EmbeddingServerWatchdog is not None
    assert SocketEmbedderClient.DEFAULT_DIMENSION == 384


def test_embedding_server_missing_sidecar_falls_back(monkeypatch):
    # Make the optional sidecar module unimportable regardless of whether it is
    # installed, so the fallback path is exercised deterministically.
    monkeypatch.setitem(sys.modules, "headroom.memory.adapters.watchdog", None)

    # Don't actually start a server.
    import headroom.proxy.server as server_mod

    monkeypatch.setattr(server_mod, "run_server", lambda *args, **kwargs: None)

    result = CliRunner().invoke(main, ["proxy", "--embedding-server", "--port", "8799"])

    assert result.exit_code == 0, f"proxy crashed instead of falling back: {result.output}"
    assert result.exception is None
    assert "Falling back to per-worker embedder" in result.output
