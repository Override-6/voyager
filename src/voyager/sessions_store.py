"""Browsing saved conversations: a small meta.json next to each session.json (so listing never parses transcripts)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionInfo:
    id: str
    cwd: str
    title: str
    updated: float
    agents: int
    turns: int  # user messages typed into the main agent

    def matches(self, query: str) -> bool:
        q = query.lower().strip()
        return not q or q in self.title.lower() or q in self.id or q in self.cwd.lower()


def first_user_text(items: list[dict[str, Any]]) -> str:
    for it in items:
        if it.get("kind") == "user" and it.get("meta", {}).get("src", "user") == "user" and it.get("text", "").strip():
            return " ".join(it["text"].split())[:120]
    return ""


def make_meta(session_id: str, cwd: str, agents: list[dict[str, Any]]) -> dict[str, Any]:
    main = next((a for a in agents if a["id"] == "main"), {"items": []})
    turns = sum(1 for i in main["items"] if i.get("kind") == "user" and i.get("meta", {}).get("src", "user") == "user")
    return {"id": session_id, "cwd": cwd, "title": first_user_text(main["items"]), "agents": len(agents), "turns": turns}


def write_meta(directory: Path, meta: dict[str, Any]) -> None:
    tmp = directory / "meta.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(directory / "meta.json")


def _read(d: Path) -> SessionInfo | None:
    sj = d / "session.json"
    try:
        updated = sj.stat().st_mtime
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):  # saved before meta.json existed: derive it once
            data = json.loads(sj.read_text())
            meta = make_meta(data["id"], data["cwd"], data["agents"])
        return SessionInfo(meta["id"], meta["cwd"], meta["title"], updated, meta["agents"], meta["turns"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def list_saved(root: Path, *, cwd: str | None = None, exclude: str | None = None) -> list[SessionInfo]:
    """Saved conversations, newest first. cwd=None lists every directory. Empty ones are never listed."""
    infos = [i for d in root.glob("*/") if (i := _read(d)) and i.turns > 0 and i.id != exclude]
    if cwd is not None:
        infos = [i for i in infos if i.cwd == cwd]
    return sorted(infos, key=lambda i: i.updated, reverse=True)


def ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= secs:
            return f"{s // secs}{unit} ago"
    return "just now"
