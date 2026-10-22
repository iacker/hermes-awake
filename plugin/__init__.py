"""hermes-awake: keep the Mac awake while Hermes works, let it sleep when it is done.

Wiring only. Logic: keeper.py (policy + actions), registry.py (cross-process lid hold),
macos.py (system calls), state.py (shared files), commands.py (user commands), tool.py (agent tool).
"""
from __future__ import annotations

import atexit
import logging
import sys
import types

from . import commands, tool
from .keeper import Keeper

log = logging.getLogger(__name__)

# The keeper outlives plugin reloads: it is parked in a module that a reload does not replace.
_runtime = sys.modules.setdefault("_hermes_awake_runtime", types.ModuleType("_hermes_awake_runtime"))


def _current() -> Keeper | None:
    return getattr(_runtime, "keeper", None)


def _guard(fn):
    """Fail open: a keep-awake problem must never break a turn."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.debug("hermes-awake: %s failed", fn.__name__, exc_info=True)
    return wrapper


LONG_LIVED = ("gateway", "dashboard")  # they honour `hold` even without turns


def _ensure_keeper() -> Keeper:
    """Started on the first turn, or at load for long-lived processes. One-shot commands
    such as `hermes awake status` never start one."""
    keeper = _current()
    if keeper is None or keeper.stopped:
        keeper = _runtime.keeper = Keeper(label=_process_label())
        keeper.start()
        atexit.register(lambda: _current() and _current().shutdown())
    return keeper


def _replace_after_reload() -> None:
    """A reload brings new code: hand the live turns over to a fresh keeper."""
    old = _current()
    if old is None or isinstance(old, Keeper):  # same class object = same code, keep it
        return
    old.shutdown()
    _runtime.keeper = None
    new = _ensure_keeper()
    new.turns = {k: list(v) if isinstance(v, (list, tuple)) else [v, v] for k, v in old.turns.items()}
    new.idle_since = old.idle_since
    new.nudge()


def _turn_key(session_id=None, turn_id=None) -> str:
    """A session runs one turn at a time, so keying by session means a turn that crashed before
    `on_session_end` is replaced by that session's next turn instead of leaking."""
    return str(session_id or turn_id or "")


@_guard
def _on_turn_start(session_id=None, turn_id=None, **_):
    if key := _turn_key(session_id, turn_id):
        _ensure_keeper().turn_started(key)


@_guard
def _on_activity(session_id=None, turn_id=None, **_):
    if (key := _turn_key(session_id, turn_id)) and _current():
        _current().turn_active(key)


@_guard
def _on_turn_end(session_id=None, turn_id=None, **_):
    if (key := _turn_key(session_id, turn_id)) and _current():
        _current().turn_ended(key)


def _apply_now() -> None:
    """After a command, decide immediately in this process instead of waiting for a tick."""
    _ensure_keeper().tick()


def _slash(raw: str = "") -> str:
    return commands.run((raw or "").split(), on_change=_guard(_apply_now))


def _cli_setup(parser) -> None:
    parser.add_argument("args", nargs="*", help="status | hold | release | pause | resume | log | config | set | doctor")


def _cli_run(ns) -> None:
    print(commands.run(list(ns.args)))


def _process_label() -> str:
    argv = " ".join(sys.argv)
    return next((name for name in ("gateway", "dashboard", "cron", "acp") if name in argv), "cli")


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_turn_start)
    ctx.register_hook("on_session_end", _on_turn_end)  # fires at the end of every turn
    for heartbeat in ("pre_api_request", "pre_tool_call", "post_tool_call"):
        ctx.register_hook(heartbeat, _on_activity)
    ctx.register_tool(name="awake", toolset="awake", schema=tool.SCHEMA,
                      handler=tool.make_handler(_guard(_apply_now)),
                      description="Keep the Mac awake for work that outlives a turn", emoji="☕")
    ctx.register_command("awake", _slash, description="Keep the Mac awake while Hermes works",
                         args_hint="[status|hold <dur> [why]|release|pause [dur]|resume|log|doctor]")
    ctx.register_cli_command("awake", "Keep the Mac awake while Hermes works",
                             _cli_setup, _cli_run, description=commands.USAGE)
    _guard(_replace_after_reload)()
    if _process_label() in LONG_LIVED:
        _guard(_ensure_keeper)()
