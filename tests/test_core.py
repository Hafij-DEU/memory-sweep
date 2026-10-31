import json
import os
import shutil
import signal
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = SourceFileLoader("memory_sweep", os.path.join(HERE, "..", "src", "memory-sweep"))
ms = module_from_spec(spec_from_loader("memory_sweep", _loader))
_loader.exec_module(ms)


def proc(pid, ppid, exe="", argv=(), comm="", uid=None, ram=1 << 20):
    return ms.Proc(pid, ppid, ms.UID if uid is None else uid, comm or os.path.basename(exe),
                   exe, list(argv), 1000.0 + pid, ram, 0)


def test_labels():
    assert ms.label(proc(1, 0, "/usr/bin/python3", ["python3", "-u", "/x/tool.py"]))[1] == "tool (python)"
    assert ms.label(proc(1, 0, "/usr/bin/python3.14", ["python3", "-m", "http.server"]))[0] == "python:http.server"
    assert ms.label(proc(1, 0, "/opt/node", ["node", "/a/.bin/rea", "mcp"]))[1] == "rea (node)"
    assert ms.label(proc(1, 0, "/usr/bin/java", ["java", "-cp", "a.jar", "org.gradle.launcher.daemon.bootstrap.GradleDaemon"]))[0] == "gradle-daemon"
    assert ms.label(proc(1, 0, "/usr/bin/java", ["java", "-Xmx1g", "-jar", "/x/app.jar"]))[1] == "app (java)"
    assert ms.label(proc(1, 0, "/snap/brave/1/opt/brave.com/brave/brave", ["brave"]))[1] == "Brave"
    assert ms.label(proc(1, 0, "/home/u/.config/Claude/claude-code/2/x/claude", ["claude"]))[0] == "claude-code"
    # unreadable exe (another user's process): fall back to argv, not the truncated comm
    assert ms.label(proc(1, 0, "", ["/usr/bin/python3", "/usr/bin/input-remapper-service"],
                         comm="input-remapper-"))[1] == "input-remapper-service (python)"
    # a process that rewrote its title into one string
    assert ms.label(proc(1, 0, "/opt/node", ["npm exec rea-agents@6 mcp"]))[0] == "npm-exec"


def test_groups_and_instances():
    procs = [
        proc(10, 1, "/opt/brave/brave", ["brave"], ram=100),
        proc(11, 10, "/opt/brave/brave", ["brave", "--type=renderer"], ram=50),
        proc(12, 10, "/opt/brave/chrome_crashpad_handler", ["crashpad"], ram=5),
        proc(20, 1, "/opt/node", ["npm exec rea-agents@6 mcp"], ram=7),
        proc(21, 20, "/bin/dash", ["sh", "-c", "rea mcp"], ram=1),
        proc(22, 21, "/opt/node", ["node", "/a/.bin/rea", "mcp"], ram=30),
        proc(30, 1, "/opt/node", ["node", "/a/.bin/rea", "mcp"], ram=40),
    ]
    groups = {g.key: g for g in ms.build_groups(procs)}
    brave = groups["brave"]
    assert len(brave.instances) == 1 and brave.ram == 150
    assert [p.pid for p in brave.instances[0].tree] == [10, 12, 11] or \
        sorted(p.pid for p in brave.instances[0].tree) == [10, 11, 12]
    rea = groups["node:rea"]
    assert len(rea.instances) == 2 and rea.ram == 78  # npm + sh wrappers folded in
    assert "npm-exec" not in groups and "dash" not in groups


def test_locks():
    procs = [proc(5, 1, "/usr/bin/gnome-shell", ["gnome-shell"]),
             proc(6, 1, "/usr/sbin/snapd", ["snapd"], uid=0),
             proc(7, 1, "/usr/bin/foo", ["foo"]),
             proc(8, 1, "/usr/bin/bar", ["bar"])]
    groups = {g.key: g for g in ms.build_groups(procs, keep=frozenset({8}))}
    assert groups["gnome-shell"].locked
    assert groups["snapd"].locked == "Runs as another user"
    assert groups["foo"].locked is None
    assert groups["bar"].locked == "Memory Sweep was started from this app"
    me = proc(os.getpid(), 1, "/usr/bin/python3", ["python3", "memory-sweep"])
    [group] = ms.build_groups([me], keep=frozenset({os.getpid()}))
    assert group.locked == "This is Memory Sweep itself"


def test_real_scan_finds_self():
    procs = ms.scan_procs()
    me = next(p for p in procs if p.pid == os.getpid())
    assert me.own and me.ram > 0 and abs(me.start - time.time()) < 3600 * 24 * 30
    assert os.getpid() in ms.self_and_ancestors()


def test_stop_ends_tree():
    # a parent shell with a child sleep; polite stop must end both
    p = subprocess.Popen(["/bin/sh", "-c", "sleep 300 & wait"])
    time.sleep(0.3)
    groups = ms.build_groups(ms.scan_procs())
    inst = next(i for g in groups for i in g.instances if i.root.pid == p.pid)
    child = [q.pid for q in inst.tree if q.pid != p.pid]
    assert child
    left = ms.stop([inst], wait=3)
    assert p.wait(timeout=5) is not None
    assert left == []
    for c in child:
        assert not ms.alive(c, next(q.start for q in inst.tree if q.pid == c))


def test_stop_respects_keep_and_pid_reuse():
    p = subprocess.Popen(["sleep", "300"])
    time.sleep(0.2)
    inst = next(i for g in ms.build_groups(ms.scan_procs()) for i in g.instances if i.root.pid == p.pid)
    ms.stop([inst], keep=frozenset({p.pid}), wait=0.5)
    assert p.poll() is None  # kept pids are never signalled
    # a stale start time must not be signalled (pid reuse guard)
    ms._send(p.pid, inst.root.start - 100, signal.SIGKILL)
    assert p.poll() is None
    p.kill()
    p.wait()


@pytest.fixture
def shm_dir():
    if not any(mp == "/dev/shm" for mp, _, _ in ms.ram_mounts()):
        pytest.skip("/dev/shm is not tmpfs")
    d = f"/dev/shm/memory-sweep-test-{os.getpid()}"
    os.makedirs(d + "/sub")
    with open(d + "/sub/f", "wb") as f:
        f.write(b"x" * 100000)
    yield d
    subprocess.run(["rm", "-rf", d])


def test_measure_and_remove(shm_dir):
    size, mtime, sockets = ms.measure(shm_dir)
    assert size >= 100000 and sockets == 0
    items = ms.list_items("/dev/shm", markers=(set(), set()))
    assert any(i.path == shm_dir and i.own and i.is_dir for i in items)
    assert ms.remove(shm_dir) == 0
    assert not os.path.exists(shm_dir)


def test_remove_refuses_outside_and_symlink_escape(tmp_path, shm_dir):
    target = tmp_path / "keep.txt"
    target.write_text("important")
    assert ms.removable(str(target)) is not None or str(tmp_path).startswith("/tmp/")
    with pytest.raises(PermissionError):
        ms.remove("/etc/hostname")
    with pytest.raises(PermissionError):
        ms.remove("/dev/shm")
    # a symlink inside a RAM folder pointing outside: removing it drops only the link
    link = shm_dir + "/link"
    os.symlink(str(target), link)
    ms.remove(link)
    assert target.read_text() == "important"
    # a symlinked parent folder must not smuggle in an outside path
    os.symlink(str(tmp_path), shm_dir + "/dirlink")
    if not str(tmp_path).startswith(("/tmp/", "/dev/shm/")):
        with pytest.raises(PermissionError):
            ms.remove(shm_dir + "/dirlink/keep.txt")
        assert target.exists()


def test_describe():
    uid = ms.UID
    assert ms.describe(f"/tmp/claude-{uid}") == "Claude Code temp files"
    assert ms.describe(f"/tmp/claude-{uid}/-home-u-apps/dfca909e-4619-483d-aecd-be8cbfb8bcfe") == \
        "Claude Code session temp files"
    assert ms.describe("/tmp/node-compile-cache").startswith("Compile cache")


def test_hidden_usage():
    it = ms.Item()
    it.path, it.size = "/dev/shm/a", 10 << 20
    assert ms.hidden_usage([it], [("/dev/shm", 100 << 20, 1 << 30)]) == {"/dev/shm": 90 << 20}
    assert ms.hidden_usage([it], [("/dev/shm", 11 << 20, 1 << 30)]) == {}


def test_cli_report_json():
    out = subprocess.run([sys.executable, os.path.join(HERE, "..", "src", "memory-sweep"), "report", "--json"],
                         capture_output=True, text=True, check=True).stdout
    import json
    data = json.loads(out)
    assert data["ram"]["total"] > 0 and data["apps"]


def test_cli_end(capsys, tmp_path):
    # a uniquely named binary, so `end` can only match this probe. Copy python3, not sleep:
    # a multicall coreutils (uutils) refuses to run under another name.
    exe = tmp_path / "ms-end-probe"
    shutil.copy(os.path.realpath(sys.executable), exe)
    p = subprocess.Popen([str(exe), "-c", "import time; time.sleep(300)"])
    time.sleep(0.5)
    assert p.poll() is None
    key = next(g.key for g in ms.build_groups(ms.scan_procs()) if any(x.pid == p.pid for x in g.procs))
    assert key == "ms-end-probe"
    assert ms.main(["end", key]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["key"] == key and out["left"] == 0
    assert p.wait(timeout=5) is not None
    assert ms.main(["end", "no-such-app-xyz"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def swap_state(**kw):
    st = {"ram": 16 << 30, "tools": True, "zram_on": True, "zram_percent": 50, "algo": "zstd",
          "zram_size": 8 << 30, "zram_used": 0, "zram_stored": 0, "zram_ram": 0,
          "file": "/swap.img", "file_size": 4 << 30, "file_used": 0, "file_active": True,
          "disk_free": 40 << 30, "zswap": False, "zswap_compressor": "zstd",
          "swappiness": 60, "cache_pressure": 100}
    st.update(kw)
    return st


def test_swap_form_matches_system():
    st = swap_state()
    form = ms.swap_form(st)
    assert form == {"tools": None, "zram": 50, "algo": "zstd", "file": 4, "zswap": False,
                    "swappiness": 60, "cache_pressure": 100}
    assert ms.plan_changes(st, form) == []
    # zram off reads as 0 whatever the config says; no swap file reads as 0 GB
    off = ms.swap_form(swap_state(zram_on=False, file_size=0))
    assert off["zram"] == 0 and off["file"] == 0


def test_plan_changes():
    st = swap_state()
    form = dict(ms.swap_form(st), zram=100, file=8, zswap=True, swappiness=150)
    assert ms.plan_changes(st, form) == ["zram=100:zstd", "file=8", "zswap=1", "swappiness=150"]
    # a new algorithm alone still restarts zram with the same size
    assert ms.plan_changes(st, dict(ms.swap_form(st), algo="lz4")) == ["zram=50:lz4"]
    # no zswap in the kernel: never ask for it
    nz = swap_state(zswap=None)
    assert ms.plan_changes(nz, dict(ms.swap_form(nz), zswap=True)) == []
    # 0 GB means "leave the swap file alone", not "delete it"
    assert ms.plan_changes(st, dict(ms.swap_form(st), file=0)) == []


def test_plan_changes_tools():
    bare = swap_state(tools=False, zram_on=False, zram_percent=0)
    form = ms.swap_form(bare)
    # without zram-tools a zram size cannot be set
    assert ms.plan_changes(bare, dict(form, zram=50)) == []
    # installing it sets the asked size straight after
    assert ms.plan_changes(bare, dict(form, tools="install", zram=50)) == ["tools=install", "zram=50:zstd"]
    st = swap_state()
    assert ms.plan_changes(st, dict(ms.swap_form(st), tools="remove", zram=80)) == ["tools=remove"]


def test_describe_change():
    assert ms.describe_change("zram=0:lz4") == "Turn zram off"
    assert ms.describe_change("zram=75:zstd") == "zram at 75% of RAM, packed with zstd"
    assert ms.describe_change("file=6") == "Swap file of 6 GB"
    assert ms.describe_change("zswap=1") == "Turn zswap on"
    assert ms.describe_change("tools=remove") == "Remove zram-tools and turn zram off"
    assert ms.describe_change("cache_pressure=50") == "Cache pressure 50"


def test_swap_command_uses_source_helper_when_not_installed():
    cmd = ms.swap_command(["file=4"])
    assert cmd[0] == "pkexec" and cmd[-1] == "file=4"
    if not os.access(ms.SWAP_HELPER, os.X_OK):
        assert cmd[1:3] == ["/bin/sh", os.path.realpath(os.path.join(HERE, "..", "src", "memory-sweep-swap"))]


def test_swap_status_reads_system():
    st = ms.swap_status()
    assert st["ram"] > 0
    assert st["algo"] in ms.ALGOS
    assert st["zswap"] in (None, True, False)
    assert 0 <= st["swappiness"] <= 200


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ms.load_settings() == ms.SETTINGS
    ms.save_settings(dict(ms.SETTINGS, refresh=10, theme="light"))
    assert ms.load_settings()["refresh"] == 10
    # wrong types and unknown keys are dropped, broken files fall back to the defaults
    (tmp_path / "memory-sweep" / "settings.json").write_text('{"refresh": "x", "bogus": 1, "notify": false}')
    s = ms.load_settings()
    assert s["refresh"] == ms.SETTINGS["refresh"] and s["notify"] is False and "bogus" not in s
    (tmp_path / "memory-sweep" / "settings.json").write_text("{")
    assert ms.load_settings() == ms.SETTINGS


@pytest.mark.parametrize("args", [[], ["bogus=1"], ["tools=purge"], ["zram=200:zstd"], ["zram=50:gzip"],
                                  ["zram=x:lz4"], ["file=0"], ["file=65"], ["file=-1"], ["swappiness=201"],
                                  ["cache_pressure=0"], ["swappiness=6a"]])
def test_helper_refuses_bad_arguments(args):
    # Each of these fails its check before the helper touches anything, so it is safe unprivileged.
    helper = os.path.join(HERE, "..", "src", "memory-sweep-swap")
    assert subprocess.run(["/bin/sh", helper, *args], capture_output=True).returncode == 2


def test_notification_methods_exist():
    # _notify runs from a GLib idle callback, so a bad method name only surfaces as an
    # apport "crash" report while the app keeps running
    import re
    from gi.repository import Gio
    src = open(_loader.path).read()
    body = src[src.index("def _notify("):]
    body = body[:body.index("\n        def ", 1)]
    calls = set(re.findall(r"\bn\.(\w+)\(", body))
    assert calls and all(hasattr(Gio.Notification, c) for c in calls), calls


MESH = "/opt/MeshDrop/meshdrop-desktop"


def meshdrop_tree():
    """MeshDrop as seen on 2026-10-31: the main process plus a second full copy
    (the clipboard helper) with its own zygote, GPU and network processes. Chromium
    rewrites its title, so each argv is one string."""
    def m(pid, ppid, args="", ram=1 << 20):
        return proc(pid, ppid, MESH, [f"{MESH} {args}".strip()], comm="meshdrop-deskto", ram=ram)
    return [
        proc(50, 1, "/usr/lib/systemd/systemd", ["/usr/lib/systemd/systemd", "--user"]),
        m(100, 50, ram=68 << 20),
        m(101, 100, "--type=zygote --no-zygote-sandbox"),
        m(102, 101, "--type=gpu-process --ozone-platform=wayland", ram=35 << 20),
        m(103, 100, "--type=zygote"),
        m(104, 103, "--type=renderer", ram=53 << 20),
        m(105, 100, "--type=utility --utility-sub-type=network.mojom.NetworkService"),
        proc(106, 100, "/usr/bin/python3.14", ["python3", "/opt/MeshDrop/resources/meshdrop-bt.py"]),
        m(110, 100, "--password-store=basic", ram=28 << 20),
        m(111, 110, "--type=zygote --no-zygote-sandbox"),
        m(112, 111, "--type=gpu-process", ram=10 << 20),
        m(113, 112, "--type=broker"),
        m(114, 110, "--type=zygote"),
        m(115, 110, "--type=utility --utility-sub-type=network.mojom.NetworkService", ram=7 << 20),
    ]


def test_extra_copy_found_for_meshdrop():
    groups = {g.key: g for g in ms.build_groups(meshdrop_tree())}
    mesh = groups["meshdrop-desktop"]
    assert mesh.title == "MeshDrop"
    [extra] = mesh.extras
    assert extra.root.pid == 110  # the clipboard helper, never the main process 100
    assert sorted(p.pid for p in extra.tree) == [110, 111, 112, 113, 114, 115]
    assert extra.ram == (28 + 1 + 10 + 1 + 1 + 7) << 20
    assert "clipboard" in mesh.extra_note
    # the launcher that started MeshDrop sees no extra copy of its own
    assert groups["systemd"].extras == []


def test_extra_copy_not_found_elsewhere():
    brave = [proc(10, 1, "/opt/brave/brave", ["/opt/brave/brave"]),
             proc(11, 10, "/opt/brave/brave", ["/opt/brave/brave --type=renderer"]),
             proc(12, 10, "/opt/brave/brave", ["/opt/brave/brave --type=gpu-process"])]
    # a shell starting a shell, a Python pool: same program, but no Chromium helpers
    shells = [proc(20, 1, "/bin/bash", ["bash"]), proc(21, 20, "/bin/bash", ["bash"]),
              proc(22, 21, "/bin/bash", ["bash"])]
    pool = [proc(30, 1, "/usr/bin/python3", ["python3", "/x/job.py"]),
            proc(31, 30, "/usr/bin/python3", ["python3", "/x/job.py"])]
    # a second main process under a helper (not under the main process) is left alone
    odd = [proc(40, 1, "/opt/x/x", ["/opt/x/x"]), proc(41, 40, "/opt/x/x", ["/opt/x/x --type=utility"]),
           proc(42, 41, "/opt/x/x", ["/opt/x/x"]), proc(43, 42, "/opt/x/x", ["/opt/x/x --type=gpu-process"])]
    for g in ms.build_groups(brave + shells + pool + odd):
        assert g.extras == [], g.key


def test_extra_copy_inside_extra_copy_counted_once():
    tree = meshdrop_tree() + [
        proc(120, 110, MESH, [f"{MESH} --nested"]),
        proc(121, 120, MESH, [f"{MESH} --type=zygote"])]
    [mesh] = [g for g in ms.build_groups(tree) if g.key == "meshdrop-desktop"]
    [extra] = mesh.extras
    assert extra.root.pid == 110 and {120, 121} <= {p.pid for p in extra.tree}


def test_extra_copy_locks():
    other = [proc(p.pid, p.ppid, p.exe, p.argv, uid=ms.UID + 1) for p in meshdrop_tree()]
    [mesh] = [g for g in ms.build_groups(other) if g.key == "meshdrop-desktop"]
    assert ms.instance_locked(mesh.extras[0]) == "Runs as another user"
    [mesh] = [g for g in ms.build_groups(meshdrop_tree()) if g.key == "meshdrop-desktop"]
    assert ms.instance_locked(mesh.extras[0], keep=frozenset({113})) == "Memory Sweep was started from this app"
    assert ms.instance_locked(mesh.extras[0]) is None


def test_extra_copy_tip(monkeypatch):
    monkeypatch.setattr(ms, "scan_procs", meshdrop_tree)
    snap = ms.Snapshot()
    [tip] = [t for t in snap.tips() if t[0] == "extra"]
    assert tip[2:] == ("apps", "meshdrop-desktop") and "MeshDrop" in tip[1] and "48 MB" in tip[1]
    # the tip needs a copy worth ending
    small = meshdrop_tree()
    for p in small:
        if p.pid >= 110:
            p.ram = 1 << 20
    monkeypatch.setattr(ms, "scan_procs", lambda: small)
    assert not [t for t in ms.Snapshot().tips() if t[0] == "extra"]


PROBE = r'''
import subprocess, sys, time
role = sys.argv[1]
def spawn(*args):
    return subprocess.Popen([sys.executable, __file__, *args])
if role == "main":
    spawn("--type=gpu-process"); spawn("--type=utility"); spawn("--password-store=basic")
elif role == "--password-store=basic":
    spawn("--type=zygote"); spawn("--type=gpu-process")
time.sleep(300)
'''


@pytest.fixture
def electron_probe(tmp_path):
    """A fake Electron app made of real processes: a main process with helpers and a
    second full copy with its own helpers. Copy python3 under a unique name."""
    exe = tmp_path / "ms-electron-probe"
    shutil.copy(os.path.realpath(sys.executable), exe)
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    main = subprocess.Popen([str(exe), str(script), "main"])
    deadline = time.time() + 10
    while time.time() < deadline:
        g = next((g for g in ms.build_groups(ms.scan_procs()) if g.key == "ms-electron-probe"), None)
        if g and len(g.procs) == 6:
            break
        time.sleep(0.2)
    yield main
    for p in ms.scan_procs():
        if p.exe == str(exe):
            os.kill(p.pid, signal.SIGKILL)
    main.wait(timeout=5)


def test_stop_extra_copy_keeps_main(electron_probe):
    [g] = [g for g in ms.build_groups(ms.scan_procs()) if g.key == "ms-electron-probe"]
    [extra] = g.extras
    assert extra.root.ppid == electron_probe.pid and len(extra.tree) == 3
    assert ms.stop([extra], wait=3) == []
    [g] = [g for g in ms.build_groups(ms.scan_procs()) if g.key == "ms-electron-probe"]
    assert electron_probe.poll() is None
    assert len(g.procs) == 3 and g.extras == []  # main, gpu and utility still run


def test_cli_end_extra(capsys, electron_probe):
    assert ms.main(["end-extra", "ms-electron-probe"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["left"] == 0 and len(out["ended"]) == 1 and "start it again" in out["note"]
    assert electron_probe.poll() is None
    assert ms.main(["end-extra", "ms-electron-probe"]) == 2  # nothing left to end
    assert "no extra copy" in json.loads(capsys.readouterr().out)["error"]
    assert ms.main(["end-extra", "no-such-app-xyz"]) == 2
    capsys.readouterr()
