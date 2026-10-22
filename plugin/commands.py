"""User commands, shared by `/awake ...` in a session and `hermes awake ...` in a shell."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict

from . import macos, registry, state
from .keeper import clock
from .state import HOLD, PAUSE, Settings

USAGE = """usage: awake <command>
  status                 what holds the Mac awake, and why (default)
  hold <duration> [why]  keep awake without work (capped by max_hold_hours)
  release                end the hold (same as `hold off`)
  pause [duration]       let the Mac sleep now (default 1h), also releases the lid
  resume                 end a pause
  log [n]                last n events (sleeps, safety stops, transitions)
  config                 show settings
  set <key> <value>      change a setting, e.g. `set battery_floor 25`
  doctor                 check the setup, print fixes
durations: 90 (minutes), 45m, 2h, 1h30m, 30s"""


def parse_duration(text: str) -> float:
    if text.isdigit():
        return int(text) * 60
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.lower())
    if not parts or not any(parts.groups()):
        raise ValueError(f"bad duration {text!r} (try 45m, 2h, 1h30m)")
    h, m, s = (int(g or 0) for g in parts.groups())
    return h * 3600 + m * 60 + s


def run(args: list[str], on_change=lambda: None) -> str:
    command, rest = (args[0], args[1:]) if args else ("status", [])
    try:
        handler = COMMANDS[command]
    except KeyError:
        return USAGE
    try:
        out = handler(rest)
    except (ValueError, KeyError, IndexError) as exc:
        return f"error: {exc}\n\n{USAGE}"
    on_change()
    return out


# -- commands ---------------------------------------------------------------------------------

def status(_: list[str]) -> str:
    s, now = Settings.load(), time.time()
    procs = registry.snapshots()
    awake = [p for p in procs if p.get("awake")]
    lid_ok = macos.lid_mode_installed()
    lines = [
        f"hermes-awake  {'● AWAKE' if awake else '○ idle'}",
        f"  power    {macos.power()}",
        f"  lid      {'installed' if lid_ok else 'not installed (doctor)'}, "
        f"SleepDisabled={'1' if macos.sleep_disabled() else '0'}, "
        f"held by {sum(bool(p.get('lid')) for p in procs)} process(es)",
    ]
    for name, label in ((HOLD, "hold"), (PAUSE, "pause")):
        if state.deadline(name) > now:
            why = state.deadline_reason(name)
            lines.append(f"  {label:<8} until {clock(state.deadline(name))}" + (f" ({why})" if why else ""))
    lines.append("  processes")
    lines += [f"    {p['pid']:>6} {p.get('label', '?'):<8} {p.get('status', '?')}"
              + "".join(f"\n             turn …{k} {age // 60} min" for k, age in (p.get("turns") or {}).items())
              for p in procs] \
        or ["    (none: no Hermes process has loaded the plugin yet)"]
    lines.append(f"  safety   floor {s.battery_floor}%, turn max {s.max_turn_hours:g}h, "
                 f"lid on battery {s.lid_battery_hours:g}h, grace {s.grace_seconds}s, "
                 f"thermal {'on' if s.thermal_stop else 'off'}")
    return "\n".join(lines)


def hold(args: list[str]) -> str:
    if args[0] == "off":
        return release([])
    cap = Settings.load().max_hold_hours * 3600
    seconds, why = min(parse_duration(args[0]), cap), " ".join(args[1:])
    state.set_deadline(HOLD, seconds, why)
    state.set_deadline(PAUSE, None)
    state.journal("hold", until=clock(state.deadline(HOLD)), why=why)
    return f"holding until {clock(state.deadline(HOLD))}" + (f" ({why})" if why else "")


def release(_: list[str]) -> str:
    held = state.deadline(HOLD) > time.time()
    state.set_deadline(HOLD, None)
    if held:
        state.journal("release")
    return "hold released" if held else "no hold to release"


def pause(args: list[str]) -> str:
    state.set_deadline(PAUSE, parse_duration(args[0]) if args else 3600)
    registry.drop_lid_votes()
    state.journal("pause", until=clock(state.deadline(PAUSE)))
    return f"paused until {clock(state.deadline(PAUSE))}: the Mac can sleep"


def resume(_: list[str]) -> str:
    state.set_deadline(PAUSE, None)
    state.journal("resume")
    return "resumed"


def show_log(args: list[str]) -> str:
    return "\n".join(state.journal_tail(int(args[0]) if args else 20)) or "(no events yet)"


def config(_: list[str]) -> str:
    return json.dumps(asdict(Settings.load()), indent=2)


def set_value(args: list[str]) -> str:
    key, value = args[0], args[1]
    Settings.load().with_value(key, value).save()
    return f"{key} = {getattr(Settings.load(), key)}"


def doctor(_: list[str]) -> str:
    procs = registry.snapshots()
    checks = [
        ("caffeinate available", macos.run(macos.CAFFEINATE, "-h")[0] in (0, 1), ""),
        ("plugin loaded in a Hermes process", bool(procs),
         "hermes plugins enable hermes-awake, then send one message"),
        ("lid-closed mode (optional helper)", macos.lid_mode_installed(),
         macos.LID_MODE_HINT),
        ("SleepDisabled consistent", macos.sleep_disabled() == any(p.get("lid") for p in procs),
         "hermes awake pause 1m, then hermes awake resume"),
    ]
    return "\n".join(f"{'✓' if ok else '✗'} {name}" + (f"\n    fix: {fix}" if not ok and fix else "")
                     for name, ok, fix in checks)


COMMANDS = {"status": status, "hold": hold, "release": release, "pause": pause, "resume": resume, "log": show_log,
            "config": config, "set": set_value, "doctor": doctor}
