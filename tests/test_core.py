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
