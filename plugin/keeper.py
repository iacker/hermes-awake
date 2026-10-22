"""The keeper: one per Hermes process. Hooks record work, a ticker thread decides and acts.

Policy, in one sentence: stay awake while there is a reason (turns, grace after a turn,
a hold set by the agent or the user) unless something vetoes it (pause, low battery, heat, lid
closed on battery for too long). `reasons` and `veto` are pure so they are easy to test.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import macos, registry, state
from .state import HOLD, PAUSE, Settings

log = logging.getLogger(__name__)


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


# -- policy (pure) --------------------------------------------------------------------------

@dataclass
class Work:
    turns: int = 0
    idle_for: float | None = None  # seconds since the last turn ended, None if never/busy
    hold_until: float = 0.0
    hold_why: str = ""


def reasons(s: Settings, w: Work, now: float) -> list[str]:
    why = []
    if w.turns:
        why.append(f"{w.turns} turn{'s' * (w.turns > 1)}")
    elif w.idle_for is not None and w.idle_for < s.grace_seconds:
        why.append(f"grace {int(s.grace_seconds - w.idle_for)}s")
    if w.hold_until > now:
        why.append(f"hold until {clock(w.hold_until)}" + (f" ({w.hold_why})" if w.hold_why else ""))
    return why


def veto(s: Settings, p: macos.Power, lid_battery_for: float) -> str | None:
    if p.on_battery and p.percent is not None and p.percent < s.battery_floor:
        return f"battery {p.percent}% < {s.battery_floor}%"
    if s.thermal_stop and p.hot:
        return "thermal pressure"
    if lid_battery_for > s.lid_battery_hours * 3600:
        return f"lid closed on battery > {s.lid_battery_hours:g}h"
    return None


# -- keeper ---------------------------------------------------------------------------------

@dataclass
class Keeper:
    label: str = "hermes"
    pid: int = field(default_factory=os.getpid)

    def __post_init__(self) -> None:
        self.settings = Settings.load()
        self.turns: dict[str, list[float]] = {}  # key -> [started, last activity]
        self.idle_since: float | None = None
        self.lid_battery_since: float | None = None
        self.status = "idle"
        self._last: tuple[bool, str | None] = (False, None)
        self._reported: tuple[str, float] = ("", 0.0)  # (status, at): skip redundant writes
        self.awake = self.lid = False
        self._caffeinate: subprocess.Popen | None = None
        self._sentinel: subprocess.Popen | None = None
        self._lid_mode: tuple[float, bool] = (0.0, False)  # (checked_at, installed)
        self._mu = threading.Lock()
        self._nudge = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # hooks: cheap, never block a turn ------------------------------------------------------
    def turn_started(self, turn_id: str) -> None:
        now = time.time()
        with self._mu:
            self.turns[turn_id] = [now, now]
            self.idle_since = None
        self._nudge.set()

    def turn_active(self, turn_id: str) -> None:
        """Heartbeat from LLM/tool calls. A turn whose end hook never fires (crash, error path)
        falls silent and expires; a turn seen for the first time (reload) is adopted."""
        with self._mu:
            if seen := self.turns.get(turn_id):
                seen[1] = time.time()
                return
        self.turn_started(turn_id)

    def turn_ended(self, turn_id: str) -> None:
        with self._mu:
            if self.turns.pop(turn_id, None) is not None and not self.turns:
                self.idle_since = time.time()
        self._nudge.set()

    # lifecycle ----------------------------------------------------------------------------
    def start(self) -> None:
        registry.sweep()  # undo whatever a crashed process left behind
        self._thread = threading.Thread(target=self._loop, name="hermes-awake", daemon=True)
        self._thread.start()
        state.journal("started", label=self.label)

    def shutdown(self) -> None:
        self._stop.set()
        self._nudge.set()
        self._set_assertion(False)
        registry.withdraw(self.pid)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def nudge(self) -> None:
        self._nudge.set()

    def _loop(self) -> None:
        last = time.time()
        while not self._stop.is_set():
            if not Path(__file__).exists():  # plugin uninstalled: give the Mac back and stop
                self.shutdown()
                break
            try:
                self.tick(last)
            except Exception:  # the loop must survive anything
                log.debug("hermes-awake tick failed", exc_info=True)
            wait = self.settings.tick_seconds * (1 if self.awake else 2)  # idle: check half as often
            nudged = self._nudge.wait(wait)
            self._nudge.clear()
            now = time.time()
            if not nudged and now - last > wait + 60:
                self._woke_up(now - last)
            last = now

    # one decision ----------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        with self._mu:
            self.settings = Settings.load()
            self._expire_hung_turns(now)
            work = Work(turns=len(self.turns),
                        idle_for=None if self.idle_since is None else now - self.idle_since,
                        hold_until=state.deadline(HOLD), hold_why=state.deadline_reason(HOLD))
            why = reasons(self.settings, work, now)
            stop = None
            if why and state.deadline(PAUSE) > now:
                stop = f"paused until {clock(state.deadline(PAUSE))}"
            elif why:
                power = macos.power()
                self._track_lid_on_battery(power, now)
                stop = veto(self.settings, power, now - (self.lid_battery_since or now))
            self._apply(awake=bool(why) and not stop, why=", ".join(why), stop=stop)

    def _expire_hung_turns(self, now: float) -> None:
        s = self.settings
        for turn_id, (started, last) in list(self.turns.items()):
            if now - started > s.max_turn_hours * 3600 or now - last > s.turn_silence_minutes * 60:
                del self.turns[turn_id]
                state.journal("turn_expired", turn=turn_id[-12:], minutes=int((now - started) // 60))
                if not self.turns:
                    self.idle_since = now

    def _track_lid_on_battery(self, p: macos.Power, now: float) -> None:
        if p.lid_closed and p.on_battery:
            self.lid_battery_since = self.lid_battery_since or now
        else:
            self.lid_battery_since = None

    def _apply(self, awake: bool, why: str, stop: str | None) -> None:
        status = f"awake: {why}" if awake else (f"stopped: {stop}" if stop else "idle")
        self._set_assertion(awake)
        if self._must_report(status, awake):
            self.lid = registry.report(self.pid, {
                "label": self.label, "status": status, "awake": awake,
                "lid": awake and self.settings.lid and self._lid_mode_installed(),
                "turns": {k[-12:]: int(time.time() - t[0]) for k, t in self.turns.items()}})
            self._reported = (status, time.time())
        if self.lid:
            self._ensure_sentinel()
        if (awake, stop) != self._last:  # journal transitions only, not every tick
            state.journal("state", status=status, lid=self.lid)
            if stop and self.settings.notify and not stop.startswith("paused"):
                macos.notify("Hermes may sleep now", f"Safety stop: {stop}")
        self._last = (awake, stop)
        self.awake, self.status = awake, status

    def _must_report(self, status: str, awake: bool) -> bool:
        """Write the registry only on a change, or to renew the lease (sooner while holding)."""
        last_status, at = self._reported
        renew = registry.LEASE_SECONDS / (5 if awake else 2)
        return status != last_status or time.time() - at > renew

    # side effects ----------------------------------------------------------------------------
    def _set_assertion(self, on: bool) -> None:
        running = self._caffeinate is not None and self._caffeinate.poll() is None
        if on and not running:
            self._caffeinate = macos.keep_awake(self.pid)
        elif not on and running:
            self._caffeinate.terminate()
            self._caffeinate.wait(timeout=2)
            self._caffeinate = None

    def _lid_mode_installed(self) -> bool:
        checked_at, installed = self._lid_mode
        if time.time() - checked_at > 600:
            self._lid_mode = (time.time(), macos.lid_mode_installed())
        return self._lid_mode[1]

    def _ensure_sentinel(self) -> None:
        """A detached watcher restores sleep if this process dies without cleaning up."""
        if (self._sentinel is not None and self._sentinel.poll() is None) or registry.sentinel_running(self.pid):
            return
        package = Path(__file__).resolve().parent
        code = ("import importlib, sys; sys.path.insert(0, sys.argv[1]);"
                "importlib.import_module(sys.argv[2] + '.registry').watch(int(sys.argv[3]))")
        self._sentinel = subprocess.Popen(
            [sys.executable, "-c", code, str(package.parent), package.name, str(self.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

    def _woke_up(self, gap: float) -> None:
        minutes = int(gap // 60)
        state.journal("slept", minutes=minutes, while_holding=self.awake)
        if self.awake and self.settings.notify:
            hint = "" if self.lid else " Lid mode off: run `hermes awake doctor`."
            macos.notify("Hermes was asleep", f"The Mac slept {minutes} min during work.{hint}")
