"""REPL driver for Python: runs code chunks in one persistent namespace, like the interactive interpreter.

Protocol (stdin): one JSON object per line, {"id": str, "code": str}. After each chunk it prints
MARK + " <id> ok|error" on its own line; everything before that is the chunk's output. Top-level `await` works
(one event loop lives across chunks). The value of a final expression is printed, as the interactive shell does.
"""

import ast
import asyncio
import json
import sys
import traceback

MARK = "\x00REPL-DONE"
FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
CO_COROUTINE = 0x80

ns: dict = {"__name__": "__main__"}
loop = asyncio.new_event_loop()


def _run(tree: ast.Module, mode: str) -> object:
    code = compile(tree if mode == "exec" else ast.Expression(tree.body[-1].value), "<repl>", mode, flags=FLAGS)
    result = eval(code, ns)
    return loop.run_until_complete(result) if code.co_flags & CO_COROUTINE else result


def run_chunk(src: str) -> bool:
    try:
        tree = ast.parse(src, "<repl>")
        last = tree.body[-1] if tree.body else None
        if isinstance(last, ast.Expr):  # show the final expression's value
            _run(ast.Module(tree.body[:-1], []), "exec")
            value = _run(ast.Module([last], []), "eval")
            if value is not None:
                print(repr(value))
        else:
            _run(tree, "exec")
        return True
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        return False


def load_file(src: str, path: str) -> bool:
    """A file's definitions go into the namespace; its `if __name__ == "__main__":` block does not run."""
    ns["__name__"] = "__repl_file__"
    try:
        exec(compile(src, path, "exec"), ns)
        return True
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        return False
    finally:
        ns["__name__"] = "__main__"


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        ok = load_file(msg["code"], msg["file"]) if msg.get("file") else run_chunk(msg["code"])
        sys.stderr.flush()
        sys.stdout.write(f"{MARK} {msg['id']} {'ok' if ok else 'error'}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
