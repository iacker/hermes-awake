# hermes-awake

A Hermes Agent plugin that keeps a Mac awake while Hermes is working and lets it sleep once the work is done. macOS only.

Hermes stops when the Mac sleeps. A kanban batch, a cron job or a build the agent is watching just freezes until someone opens the lid. This plugin holds the Mac awake for exactly as long as there is work, then gives it back.

## How it decides

Every agent turn keeps the Mac awake, in every profile, plus 90 seconds between turns so chained work doesn't lose it. A turn counts as alive while it calls the model or a tool, and one that goes quiet for 15 minutes is dropped, so a turn that crashed can't hold the Mac forever.

For work that outlives a turn, the agent gets an `awake` tool. It calls `hold` with a duration and a reason before a long build, a CI run or a batch of subagents, and `release` when that's done. A hold never lasts more than 12 hours, even if the release is forgotten.

Safety stops win over everything. The plugin lets the Mac sleep when the battery drops under 20 %, when macOS reports thermal pressure, after 3 hours on battery with the lid closed, or when a single turn has run for 3 hours. Each stop posts a notification. So does waking up from a sleep that happened in the middle of work.

## Install

```sh
git clone https://github.com/iacker/hermes-awake && cd hermes-awake
./install.sh            # every profile, or ./install.sh ares node1
```

The plugin goes to `~/.hermes/plugins/hermes-awake` once, and each profile gets a symlink to it. `./uninstall.sh` removes it everywhere. Keepers that are still running notice it, release the Mac and stop.

## Lid closed (optional)

With the lid open, the plugin only uses `caffeinate`, which needs no privileges. Staying awake with the lid closed on battery needs `pmset -a disablesleep 1`, which is a root command. The plugin itself never asks for privileges. If you want this mode, run `lid-mode/install.sh` once. It adds a sudoers rule limited to `pmset -a disablesleep 0` and `1`, and a small helper the plugin calls with `on`, `off` or `check`. `lid-mode/uninstall.sh` removes both.

The previous `SleepDisabled` value comes back when the last Hermes process stops holding. A detached sentinel per process restores it even if Hermes is killed with SIGKILL.

## Commands

The agent uses the `awake` tool. You can use `/awake` in a session or `hermes awake` in a shell.

```
status | hold <duration> [reason] | release | pause [duration] | resume | log [n] | config | set <key> <value> | doctor
```

## What it touches

It runs `caffeinate`, `pmset -g`, `ioreg` and `osascript` (for notifications), plus the lid helper if you installed it. Each Hermes process gets one background thread, which wakes every 60 seconds when idle and only reads two small files. State, settings and a journal capped at 512 KB live in `~/.hermes/hermes-awake/`. It makes no network calls.

## Tests

```sh
cd plugin && python3 -m unittest discover -s tests -v
```

The tests use only the standard library. They run a real `caffeinate`, fake the lid helper, and never change the real `SleepDisabled` setting.

## Credit

The idea comes from [kshitijk4poor/hermes-caffeinate](https://github.com/kshitijk4poor/hermes-caffeinate) (MIT). This is a rewrite as a native Python plugin.

## License

MIT
