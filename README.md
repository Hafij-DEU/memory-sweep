# Memory Sweep

A GTK4/libadwaita app for Ubuntu that shows what fills your RAM and lets you free it.

The window has a dark (or light) navy theme with tabs on top and a sidebar on the left. It fits
narrow windows too: the columns stack and the sidebar folds away.

- **Overview**: cards for RAM, swap, zram and files in RAM, a live usage chart, suggestions
  (idle build daemons, old Claude Code sessions, big browsers, large temp files) and the biggest
  apps.
- **Apps**: processes grouped by app. Multi-process apps (browsers, Electron) are measured with
  PSS, so shared memory is not counted twice. Expand an app to see each running instance with its
  PID, age and command. **End** asks it to quit (SIGTERM to the main process, then to leftover
  children); if it keeps running, the toast offers **Force quit** (SIGKILL).
- **Files in RAM**: `/tmp` and `/dev/shm` are tmpfs, so their files take RAM (or swap). Browse
  them biggest first and remove your own files. Folders that a running program seems to use are
  marked "in use".
- **Settings > Memory Optimization**: set up zram (size as a percent of RAM, compression
  lz4/zstd/lzo-rle), the swap file size, zswap, swappiness and cache pressure, and install or
  remove zram-tools. The page shows the current status and lists the pending changes; **Apply**
  runs them all as root with one password prompt. **Reset to defaults** fills in 50% zram with
  zstd, a 4 GB swap file and the kernel's vm values.

The changes run through a small helper, `/usr/libexec/memory-sweep/memory-sweep-swap`, started by
`pkexec` under its own polkit action (`io.github.hafij_bge.MemorySweep.swap`). It accepts only
known keys and ranges and refuses to turn off a swap device when what it holds would not fit back
in RAM. Its exit codes: 2 bad arguments, 3 not enough free disk space, 4 not enough free RAM,
5 btrfs swap file, 6 zram-tools not installed, 7 kernel without zswap, 8 apt-get failed. It writes
`/etc/sysctl.d/90-memory-sweep.conf` and `/etc/tmpfiles.d/memory-sweep-zswap.conf`; delete them to
undo the vm and zswap settings.

The launcher's right-click menu opens Apps, Files in RAM or Memory Optimization directly.

Safety: the app never ends processes your desktop session needs (gnome-shell, PipeWire, D-Bus,
portals, FUSE mounts, …), processes of other users, or itself. It only removes files you own in
`/tmp` and `/dev/shm`, never follows symlinks, and always asks first.

## Command line

```
memory-sweep                    open the window
memory-sweep --page PAGE        open on overview, apps, files, settings, memory or about
memory-sweep report [--json]    print a summary
memory-sweep end KEY [--force]  end one app (KEY from report --json)
```

## Build

```
./build-deb.sh                  # dist/memory-sweep_<version>_all.deb
python3 -m pytest tests
```

## Author

Developed by Md. Hafijur Rahman. MIT License.

## AI statement

Memory Sweep was built with Vibe Coding. The author described the goal, constraints and acceptance
criteria in natural language. An AI coding agent (Claude Code, Anthropic, model Claude Opus) then
inspected the system, wrote the code and tests, ran them, and fixed defects. The author reviewed
the results and gave follow-up instructions.
