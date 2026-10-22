"""The `awake` agent tool: lets the agent keep the Mac awake for work that outlives its turn."""
from __future__ import annotations

from typing import Any, Callable

from . import commands

SCHEMA = {
    "name": "awake",
    "description": (
        "Keep this Mac awake (lid closed included) while work continues outside your turn. "
        "Your own turns are already covered automatically, plus 90 s between turns. "
        "Call hold BEFORE work that keeps running after you answer or that you must come back to: "
        "a background build/training/crawl, waiting on CI, a subagent or kanban batch, a cron you "
        "watch, or the user leaving and wanting to reach you from their phone. "
        "Give the shortest duration that covers it (max 12h) and a short reason. "
        "Call release as soon as that work is done. "
        "Never hold just because a conversation is open. "
        "Safety stops (battery < 20%, heat, lid closed on battery > 3h) override any hold."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "hold", "release", "pause", "resume", "doctor"],
                "description": "status: what keeps the Mac awake. hold: keep awake for `duration`. "
                               "release: end your hold. pause: let it sleep now (user asked). "
                               "resume: end a pause. doctor: check setup.",
            },
            "duration": {
                "type": "string",
                "description": "For hold/pause: 45m, 2h, 1h30m, or minutes as a number.",
            },
            "reason": {
                "type": "string",
                "description": "For hold: what you are waiting for, e.g. 'vllm build', 'CI on PR 812'.",
            },
        },
        "required": ["action"],
    },
}


def make_handler(on_change: Callable[[], None]) -> Callable[..., str]:
    def handle(args: dict[str, Any], **_: Any) -> str:
        action = str(args.get("action") or "status")
        duration = str(args.get("duration") or "").strip()
        reason = " ".join(str(args.get("reason") or "").split())
        if action == "hold":
            if not duration or not reason:
                return "error: hold needs a duration and a reason"
            argv = ["hold", duration, *reason.split()]
        elif action == "pause":
            argv = ["pause", duration] if duration else ["pause"]
        elif action in ("status", "release", "resume", "doctor"):
            argv = [action]
        else:
            return f"error: unknown action {action!r}"
        out = commands.run(argv, on_change=on_change)
        return out if action in ("status", "doctor") else f"{out}\n\n{commands.run(['status'])}"
    return handle
