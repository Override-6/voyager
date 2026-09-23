"""File tools: read_file, write_file, edit_file."""

from __future__ import annotations

import difflib
from typing import Any

from .base import Tool, ToolContext, ToolError, clip

MAX_LINE_CHARS = 2000


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2
    )
    return "\n".join(lines)


class ReadFile(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="read_file",
            description=(
                "Read a text file. Returns lines prefixed with their 1-based line number. "
                "Use offset/limit to page through large files."
            ),
            properties={
                "path": {"type": "string", "description": "File path (absolute or relative to the working directory)"},
                "offset": {"type": "integer", "description": "First line to read (1-based). Default 1"},
                "limit": {"type": "integer", "description": "Max lines to read. Default 2000"},
            },
            required=["path"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        p = ctx.resolve(args["path"])
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        if p.is_dir():
            raise ToolError(f"{p} is a directory; use glob or bash `ls`")
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{p} is not a UTF-8 text file")
        lines = text.splitlines()
        offset = max(int(args.get("offset") or 1), 1)
        limit = max(int(args.get("limit") or 2000), 1)
        chunk = lines[offset - 1 : offset - 1 + limit]
        if not chunk:
            return f"(empty: file has {len(lines)} lines)"
        out = "\n".join(
            f"{i:>6}\t{line[:MAX_LINE_CHARS]}" for i, line in enumerate(chunk, start=offset)
        )
        end = offset + len(chunk) - 1
        if end < len(lines):
            out += f"\n… ({len(lines) - end} more lines; call again with offset={end + 1})"
        return clip(out)


class WriteFile(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="write_file",
            description=(
                "Create a file or completely overwrite an existing one. "
                "For small changes to an existing file prefer edit_file."
            ),
            properties={
                "path": {"type": "string", "description": "File path (absolute or relative to the working directory)"},
                "content": {"type": "string", "description": "Full new file content"},
            },
            required=["path", "content"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        p = ctx.resolve(args["path"])
        new = str(args["content"])
        if p.exists() and p.is_file():
            try:
                return _diff(args["path"], p.read_text(encoding="utf-8"), new) or "(no changes)"
            except UnicodeDecodeError:
                return None
        return f"new file, {len(new.splitlines())} lines:\n" + "\n".join(
            f"+{line}" for line in new.splitlines()[:40]
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        p = ctx.resolve(args["path"])
        if p.is_dir():
            raise ToolError(f"{p} is a directory")
        content = str(args["content"])
        if not content.strip():
            raise ToolError(
                "content is empty. Pass the file's real content, or if you actually want an empty file "
                "use bash (`touch` / `> file`) instead of write_file."
            )
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"{'Overwrote' if existed else 'Created'} {p} ({len(content.splitlines())} lines)"


class EditFile(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="edit_file",
            description=(
                "Replace an exact string in an existing file. old_string must match the file "
                "exactly (including whitespace) and be unique unless replace_all is true. "
                "Read the file first."
            ),
            properties={
                "path": {"type": "string", "description": "File path"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false"},
            },
            required=["path", "old_string", "new_string"],
        )

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def _apply(self, args: dict[str, Any], ctx: ToolContext) -> tuple[str, str]:
        p = ctx.resolve(args["path"])
        if not p.is_file():
            raise ToolError(f"{p} does not exist (use write_file to create it)")
        old, new = str(args["old_string"]), str(args["new_string"])
        if old == new:
            raise ToolError("old_string and new_string are identical")
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ToolError("old_string not found in file (check exact whitespace; re-read the file)")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(
                f"old_string appears {count} times; add more context to make it unique or set replace_all"
            )
        return text, text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        try:
            before, after = self._apply(args, ctx)
        except ToolError:
            return None  # run() will report the error to the model; no point prompting
        return _diff(args["path"], before, after)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        _, after = self._apply(args, ctx)
        ctx.resolve(args["path"]).write_text(after, encoding="utf-8")
        return f"Edited {ctx.resolve(args['path'])}"
