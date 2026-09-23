import asyncio
from pathlib import Path

import pytest

from voyager.local import LocalAgent
from voyager.config import Config
from voyager.session import Session


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    # never the real `claude`: tests must not talk to Anthropic
    return Config(cwd=tmp_path, sessions_dir=tmp_path / "sessions", history_path=tmp_path / "history",
                  claude_bin=str(Path(__file__).parent / "fake_claude.py"),
                  workspaces_dir=tmp_path / "workspaces", launch_cwd=tmp_path)


@pytest.fixture
def offline(monkeypatch):
    """Agents never call the model: run_turn just records what it was given."""
    seen: list[tuple[str, str]] = []

    async def fake_turn(self, text):
        seen.append((self.id, text))
        self.last_report = f"report from {self.id}"
        await asyncio.sleep(0)

    monkeypatch.setattr(LocalAgent, "run_turn", fake_turn)
    return seen


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def session(cfg):
    return Session(cfg)
