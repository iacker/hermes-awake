"""Cross-process registry and the refcounted lid hold.

Each Hermes process publishes procs/<pid>.json every tick: what it does and whether it
votes for the lid hold. The file carries a lease; a process that dies or hangs stops
renewing it and is dropped. `pmset disablesleep` is machine-wide, so it is set to 1 while
at least one live process votes for it, and the value found before the first vote (the
"prior") is restored when the last vote goes away. All of it under one flock.
"""
from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import macos, state

LEASE_SECONDS = 300


def _procs() -> Path:
    path = state.root() / "procs"
    path.mkdir(exist_ok=True)
    return path


@contextmanager
def _locked():
    with open(state.root() / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _live() -> list[dict]:
    """Snapshots of live processes; drops dead, expired or corrupt ones. Caller holds the lock."""
    snapshots = []
    for path in _procs().glob("*.json"):
        snap = _read(path)
        if snap and snap.get("lease_until", 0) > time.time() and macos.alive(int(path.stem)):
            snapshots.append(snap)
        else:
            path.unlink(missing_ok=True)
            path.with_suffix(".sentinel").unlink(missing_ok=True)
    return snapshots


def _settle() -> bool:
    """Apply the lid votes to pmset. Returns True when the lid hold is in force."""
    prior = state.root() / "prior"
    voters = [s for s in _live() if s.get("lid")]
    if voters:
        if not prior.exists():  # an orphaned prior (crash) is kept: it is the user's real value
            prior.write_text("1" if macos.sleep_disabled() else "0")
        if macos.sleep_disabled() or macos.set_sleep_disabled(True):
            return True
        prior.unlink(missing_ok=True)
        return False
    if prior.exists():
        macos.set_sleep_disabled(prior.read_text().strip() == "1")
        prior.unlink()
    return False


def report(pid: int, snapshot: dict) -> bool:
    """Publish this process's snapshot and settle the lid. Returns whether the lid is held."""
    with _locked():
        path = _procs() / f"{pid}.json"
        path.write_text(json.dumps({**snapshot, "pid": pid,
                                    "lease_until": time.time() + LEASE_SECONDS}))
        held = _settle()
        if snapshot.get("lid") and not held:  # vote failed (no lid helper): do not keep it
            path.write_text(json.dumps({**snapshot, "pid": pid, "lid": False,
                                        "lease_until": time.time() + LEASE_SECONDS}))
        return held and bool(snapshot.get("lid"))


def withdraw(pid: int) -> None:
    with _locked():
        (_procs() / f"{pid}.json").unlink(missing_ok=True)
        _settle()


def sweep() -> None:
    """Drop dead processes and restore sleep if nobody holds it (startup, sentinel)."""
    with _locked():
        _settle()


def drop_lid_votes() -> None:
    """Emergency release: clear every vote now (live keepers re-vote only if allowed)."""
    with _locked():
        for path in _procs().glob("*.json"):
            snap = _read(path)
            if snap and snap.get("lid"):
                path.write_text(json.dumps({**snap, "lid": False}))
        _settle()


def snapshots() -> list[dict]:
    with _locked():
        return _live()


def votes_for_lid(pid: int) -> bool:
    snap = _read(_procs() / f"{pid}.json")
    return bool(snap and snap.get("lid") and snap.get("lease_until", 0) > time.time())


def sentinel_running(pid: int) -> bool:
    """True when a sentinel already holds the flock for `pid`."""
    try:
        with open(_procs() / f"{pid}.sentinel", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
    except OSError:
        return True


def watch(pid: int, period: float = 10.0) -> None:
    """Sentinel body, run in a detached interpreter: restore sleep even if `pid` is SIGKILLed.
    One sentinel per watched pid: a duplicate finds the flock taken and exits at once."""
    with open(_procs() / f"{pid}.sentinel", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        while macos.alive(pid) and votes_for_lid(pid):
            time.sleep(period)
        sweep()
