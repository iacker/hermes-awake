"""Run from the plugin directory: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_DIR.parent))
os.environ["HERMES_AWAKE_DIR"] = tempfile.mkdtemp(prefix="awake-test-")

awake = importlib.import_module(PKG_DIR.name)
keeper = importlib.import_module(f"{PKG_DIR.name}.keeper")
macos = importlib.import_module(f"{PKG_DIR.name}.macos")
registry = importlib.import_module(f"{PKG_DIR.name}.registry")
state = importlib.import_module(f"{PKG_DIR.name}.state")
commands = importlib.import_module(f"{PKG_DIR.name}.commands")
tool = importlib.import_module(f"{PKG_DIR.name}.tool")

S = state.Settings()
AC = macos.Power()


class FakePmset:
    """In-process stand-in for the privileged part of pmset."""

    def __init__(self, value=False, allowed=True):
        self.value, self.allowed, self.calls = value, allowed, []

    def __enter__(self):
        self._saved = macos.sleep_disabled, macos.set_sleep_disabled
        macos.sleep_disabled = lambda: self.value
        macos.set_sleep_disabled = self.set
        return self

    def set(self, on):
        self.calls.append(on)
        if self.allowed:
            self.value = on
        return self.allowed

    def __exit__(self, *_):
        macos.sleep_disabled, macos.set_sleep_disabled = self._saved


def fresh_state():
    for path in sorted(state.root().rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()


class Policy(unittest.TestCase):
    def test_reasons(self):
        now = time.time()
        self.assertEqual(keeper.reasons(S, keeper.Work(), now), [])
        self.assertEqual(keeper.reasons(S, keeper.Work(turns=2), now), ["2 turns"])
        self.assertEqual(keeper.reasons(S, keeper.Work(idle_for=30), now), ["grace 60s"])
        self.assertEqual(keeper.reasons(S, keeper.Work(idle_for=91), now), [])
        self.assertTrue(keeper.reasons(S, keeper.Work(hold_until=now + 60), now)[0].startswith("hold"))

    def test_veto(self):
        self.assertIsNone(keeper.veto(S, AC, 0))
        self.assertIn("battery 15%", keeper.veto(S, macos.Power(on_battery=True, percent=15), 0))
        self.assertIsNone(keeper.veto(S, macos.Power(on_battery=False, percent=15), 0))
        self.assertEqual(keeper.veto(S, macos.Power(hot=True), 0), "thermal pressure")
        self.assertIn("lid closed", keeper.veto(S, AC, 4 * 3600))

    def test_thermal_parser(self):
        self.assertFalse(macos._is_hot("Note: No thermal warning level has been recorded"))
        self.assertTrue(macos._is_hot("CPU_Speed_Limit \t= 50"))
        self.assertFalse(macos._is_hot("CPU_Speed_Limit \t= 100"))


class SettingsAndCommands(unittest.TestCase):
    def setUp(self):
        fresh_state()

    def test_settings_roundtrip_and_coercion(self):
        S.with_value("battery_floor", "25").with_value("lid", "off").save()
        loaded = state.Settings.load()
        self.assertEqual((loaded.battery_floor, loaded.lid), (25, False))
        with self.assertRaises(KeyError):
            S.with_value("nope", "1")

    def test_durations(self):
        self.assertEqual(commands.parse_duration("90"), 5400)
        self.assertEqual(commands.parse_duration("1h30m"), 5400)
        self.assertEqual(commands.parse_duration("30s"), 30)
        with self.assertRaises(ValueError):
            commands.parse_duration("soon")

    def test_hold_pause_resume(self):
        commands.run(["hold", "2h"])
        self.assertGreater(state.deadline(state.HOLD), time.time() + 7000)
        commands.run(["pause", "10m"])
        self.assertGreater(state.deadline(state.PAUSE), time.time())
        commands.run(["resume"])
        self.assertEqual(state.deadline(state.PAUSE), 0.0)
        self.assertIn("usage", commands.run(["bogus"]))

    def test_hold_reason_cap_release(self):
        out = commands.run(["hold", "99h", "vllm", "build"])
        self.assertIn("(vllm build)", out)
        self.assertLess(state.deadline(state.HOLD), time.time() + 12 * 3600 + 5, "capped at 12h")
        self.assertEqual(state.deadline_reason(state.HOLD), "vllm build")
        self.assertIn("(vllm build)", commands.run(["status"]))
        self.assertEqual(commands.run(["release"]), "hold released")
        self.assertEqual(commands.run(["release"]), "no hold to release")

    def test_agent_tool(self):
        calls = []
        handle = tool.make_handler(lambda: calls.append(1))
        self.assertIn("needs a duration and a reason", handle({"action": "hold", "duration": "1h"}))
        out = handle({"action": "hold", "duration": "45m", "reason": "CI on PR 812"})
        self.assertTrue(out.startswith("holding until") and "(CI on PR 812)" in out and calls)
        self.assertIn("hold released", handle({"action": "release"}))
        self.assertIn("hermes-awake", handle({"action": "status"}))
        self.assertIn("error", handle({"action": "explode"}))
        self.assertEqual(tool.SCHEMA["name"], "awake")


class Registry(unittest.TestCase):
    def setUp(self):
        fresh_state()

    def test_refcount_across_processes(self):
        with FakePmset() as pm:
            self.assertTrue(registry.report(os.getpid(), {"lid": True}))
            self.assertTrue(registry.report(1, {"lid": True}))  # launchd: always alive
            registry.withdraw(os.getpid())
            self.assertTrue(pm.value, "another process still holds")
            registry.withdraw(1)
            self.assertFalse(pm.value, "last one out restores")

    def test_dead_process_is_reaped_and_prior_restored(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        with FakePmset() as pm:
            registry.report(dead.pid, {"lid": True})
            registry.sweep()
            self.assertFalse(pm.value)
            self.assertFalse((state.root() / "prior").exists())

    def test_user_baseline_is_preserved(self):
        with FakePmset(value=True) as pm:
            registry.report(os.getpid(), {"lid": True})
            registry.withdraw(os.getpid())
            self.assertTrue(pm.value, "user had SleepDisabled=1 before us")

    def test_no_helper_means_no_vote(self):
        with FakePmset(allowed=False):
            self.assertFalse(registry.report(os.getpid(), {"lid": True}))
            self.assertFalse(registry.votes_for_lid(os.getpid()))

    def test_pause_drops_votes(self):
        with FakePmset() as pm:
            registry.report(os.getpid(), {"lid": True})
            commands.run(["pause", "1m"])
            self.assertFalse(pm.value)

    def test_one_sentinel_per_pid(self):
        import fcntl
        self.assertFalse(registry.sentinel_running(4242))
        with open(state.root() / "procs" / "4242.sentinel", "a") as fh:  # a sentinel holds it
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(registry.sentinel_running(4242))
            started = time.time()
            registry.watch(4242)  # a duplicate returns at once instead of looping
            self.assertLess(time.time() - started, 1)


class KeeperLive(unittest.TestCase):
    """Real caffeinate, fake lid (no helper needed)."""

    def setUp(self):
        fresh_state()
        state.Settings(grace_seconds=2, lid=False).save()

    def assertion_pids(self):
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        return f"on behalf of Process ID {os.getpid()}" in out

    def test_turn_lifecycle(self):
        k = keeper.Keeper(label="test")
        k.turn_started("t1"); k.tick()
        self.assertTrue(k.awake); time.sleep(0.5)
        self.assertTrue(self.assertion_pids(), "caffeinate assertion visible in pmset")
        k.turn_ended("t1"); k.tick()
        self.assertTrue(k.awake and "grace" in k.status)
        time.sleep(2.2); k.tick()
        self.assertFalse(k.awake); time.sleep(0.5)
        self.assertFalse(self.assertion_pids())
        k.shutdown()

    def test_hung_turn_expires(self):
        k = keeper.Keeper(label="test")
        k.turns["old"] = [time.time() - 5 * 3600] * 2  # > max_turn_hours
        k.turns["silent"] = [time.time() - 600, time.time() - 20 * 60]  # no activity for 20 min
        k.turns["busy"] = [time.time() - 600, time.time()]
        k.tick()
        self.assertEqual(list(k.turns), ["busy"])
        k.turn_active("busy"); k.turn_active("adopted")
        self.assertEqual(sorted(k.turns), ["adopted", "busy"])
        self.assertIn("turn_expired", "".join(state.journal_tail()))
        k.shutdown()

    def test_hold_and_pause(self):
        k = keeper.Keeper(label="test")
        commands.run(["hold", "10m", "ci"]); k.tick()
        self.assertTrue(k.awake and "(ci)" in k.status)
        commands.run(["pause", "5m"]); k.tick()
        self.assertFalse(k.awake)
        self.assertIn("paused", k.status)
        k.shutdown()

    def test_idle_ticks_do_not_write(self):
        k = keeper.Keeper(label="test")
        k.tick()
        proc_file = state.root() / "procs" / f"{os.getpid()}.json"
        written = proc_file.stat().st_mtime_ns
        time.sleep(0.05)
        for _ in range(5):
            k.tick()
        self.assertEqual(proc_file.stat().st_mtime_ns, written, "no rewrite while nothing changes")
        k.turn_started("t"); k.tick()
        self.assertNotEqual(proc_file.stat().st_mtime_ns, written, "a change is published")
        k.shutdown()


class Sentinel(unittest.TestCase):
    """SIGKILL a holder: the detached sentinel must restore sleep. Fake lid helper."""

    def test_sigkill_restores(self):
        fresh_state()
        record = state.root() / "disablesleep"
        helper = state.root() / "lid-helper"
        helper.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$1" in on) echo 1 > {record} ;; off) echo 0 > {record} ;; esac
            """))
        helper.chmod(0o755)
        env = dict(os.environ)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""\
            import importlib, sys, time
            sys.path.insert(0, {str(PKG_DIR.parent)!r})
            k = importlib.import_module({PKG_DIR.name!r} + ".keeper")
            s = importlib.import_module({PKG_DIR.name!r} + ".state")
            m = importlib.import_module({PKG_DIR.name!r} + ".macos")
            m.sleep_disabled = lambda: False  # isolate from the real machine state
            s.Settings(lid=True).save()
            keeper = k.Keeper(label="victim")
            keeper.turn_started("t"); keeper.tick()
            print("held" if keeper.lid else "not-held", flush=True)
            time.sleep(60)
            """)], env=env, stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertEqual(record.read_text().strip(), "1")
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()
        holder.stdout.close()
        deadline = time.time() + 20
        while time.time() < deadline and record.read_text().strip() != "0":
            time.sleep(0.5)
        self.assertEqual(record.read_text().strip(), "0", "sentinel restored sleep after SIGKILL")


if __name__ == "__main__":
    unittest.main()
