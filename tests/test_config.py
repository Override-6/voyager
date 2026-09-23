import io
import json

import pytest

from voyager import cli, config
from voyager.config import server_context_window


def fake_urlopen(payload=None, exc=None):
    def _open(url, timeout):
        assert url.endswith("/props")
        if exc:
            raise exc
        return io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    return _open


def test_reads_slot_n_ctx_from_props(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen({"default_generation_settings": {"n_ctx": 131072}}))
    assert server_context_window("http://127.0.0.1:8080/") == 131072


@pytest.mark.parametrize("payload,exc", [
    (None, OSError("connection refused")),
    (b"not json", None),
    ({"default_generation_settings": {}}, None),
    ({"default_generation_settings": {"n_ctx": 0}}, None),
])
def test_unreadable_props_gives_none(monkeypatch, payload, exc):
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen(payload, exc))
    assert server_context_window("http://x") is None


def test_cli_sizes_context_from_server(monkeypatch):
    monkeypatch.setattr(cli, "server_context_window", lambda url: 131072)
    assert cli.build_config(cli.build_parser().parse_args([])).context_window == 131072


def test_cli_flag_wins_without_asking_server(monkeypatch):
    def boom(url):
        raise AssertionError("server must not be queried when --context-window is given")
    monkeypatch.setattr(cli, "server_context_window", boom)
    assert cli.build_config(cli.build_parser().parse_args(["--context-window", "4096"])).context_window == 4096


def test_cli_falls_back_with_warning(monkeypatch, capsys):
    monkeypatch.setattr(cli, "server_context_window", lambda url: None)
    cfg = cli.build_config(cli.build_parser().parse_args([]))
    assert cfg.context_window == config.Config().context_window
    assert "could not read the context size" in capsys.readouterr().err
