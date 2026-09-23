"""DaemonBackend: the TUI's sessions run in background daemons and the TUI is only ever attached to them."""

from __future__ import annotations

from ..config import Config
from .client import RemoteSession
from .launcher import find_live, spawn


def resolve_saved(cfg: Config, ref: str | None) -> str:
    """Full id of a saved session (a unique prefix is enough; no ref: the latest for this directory)."""
    from ..sessions_store import list_saved

    root = cfg.sessions_dir
    if ref:
        found = [d.name for d in root.glob(f"{ref}*/") if (d / "session.json").is_file()]
        if ref in found:
            return ref
        if len(found) > 1:
            raise FileNotFoundError(f"ambiguous session id {ref!r}: " + ", ".join(sorted(found)[:5]))
        if found:
            return found[0]
        raise FileNotFoundError(f"no such session: {ref}")
    infos = list_saved(root, cwd=str(cfg.cwd))
    if not infos:
        raise FileNotFoundError(f"no saved session for {cfg.cwd} under {root}")
    return infos[0].id


class DaemonBackend:
    async def new_session(self, cfg: Config) -> RemoteSession:
        live = await spawn(cfg)
        return await RemoteSession(cfg, live.sock).connect()

    async def load_session(self, cfg: Config, ref: str) -> RemoteSession:
        """Attach to the session's daemon if it is running, else start one that resumes the saved session."""
        live = find_live(cfg.sessions_dir, ref) or await spawn(cfg, resume=resolve_saved(cfg, ref))
        return await RemoteSession(cfg, live.sock).connect()


async def open_first(cfg: Config, *, prompt: str | None, resume: str | None, use_latest: bool) -> RemoteSession:
    """The session the TUI starts on: a new one (with `prompt`), or a resumed one (live daemon first, else a new daemon)."""
    if resume is not None or use_latest:
        sid = resolve_saved(cfg, resume or None)
        live = find_live(cfg.sessions_dir, sid) or await spawn(cfg, resume=sid, prompt=prompt)
    else:
        live = await spawn(cfg, prompt=prompt)
    return await RemoteSession(cfg, live.sock).connect()
