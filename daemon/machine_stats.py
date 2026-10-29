"""Machine meter sampler: CPU load, free RAM and which MNIIS rigs are up.

One background thread wakes every SAMPLE_S seconds and reads two kernel
counters (GetSystemTimes, GlobalMemoryStatusEx); every RIG_S seconds it also
re-reads the stack.env files and probes each rig's ports with a 50 ms TCP
connect to 127.0.0.1. Pure stdlib + ctypes: no psutil, no WMI, no subprocess.

Rig state never comes from docker or wsl.exe. dockerd lives in the Ubuntu WSL
distro and any docker or wsl call boots the VM when it is stopped; a refused
connect to a forwarded port does not.

Listeners are called from the sampler thread only when a rounded value
changed, so the overlay and the BLE payload redraw or resend nothing on a
flat machine.
"""
import ctypes
import os
import select
import socket
import sys
import threading
from typing import Callable, NamedTuple

SAMPLE_S = 5.0
RIG_S = 60.0
PROBE_TIMEOUT_S = 0.05

# Folders rigs.mjs (~/.claude/bin/rigs.mjs) scans. The primary checkout's
# worktrees come from its .git/worktrees/*/gitdir files instead of
# `git worktree list`, so discovery spawns no process.
MAIN_REPO = r"C:\Users\DannyHearit\RiderProjects\mniis"
RIG_BASES = (r"C:\Users\DannyHearit\RiderProjects", r"C:\wt")
PRIMARY_RIG = ("main", (5432, 8080, 5173))

MAX_RIGS = 8
RIG_NAME_LEN = 8

RIG_DOWN, RIG_UP, RIG_PARTIAL = 0, 1, 2


class Snapshot(NamedTuple):
    cpu: int                 # 0-100, all cores
    ram_free_gb: float       # one decimal
    ram_used_pct: int        # 0-100
    rigs: tuple              # ((name, state), ...) up first, then partial, then down


# ---------------------------------------------------------------------------
# Kernel counters
# ---------------------------------------------------------------------------

class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

    def ticks(self) -> int:
        return (self.hi << 32) | self.lo


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def system_times() -> tuple[int, int, int] | None:
    """(idle, kernel, user) 100 ns ticks since boot; kernel includes idle."""
    if sys.platform != "win32":
        return None
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return idle.ticks(), kernel.ticks(), user.ticks()


def cpu_pct(prev: tuple[int, int, int], cur: tuple[int, int, int]) -> int:
    """Busy percent across all cores between two system_times() reads."""
    d_idle = cur[0] - prev[0]
    d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])
    if d_total <= 0:
        return 0
    return max(0, min(100, round(100 * (d_total - d_idle) / d_total)))


def memory() -> tuple[float, int] | None:
    """(free GB with one decimal, used percent) of physical RAM."""
    if sys.platform != "win32":
        return None
    ms = _MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return None
    return ram_figures(ms.ullAvailPhys, ms.ullTotalPhys)


def ram_figures(avail: int, total: int) -> tuple[float, int]:
    if total <= 0:
        return 0.0, 0
    return round(avail / 2**30, 1), round(100 * (total - avail) / total)


# ---------------------------------------------------------------------------
# Rigs
# ---------------------------------------------------------------------------

def parse_stack_env(text: str) -> dict:
    """MIIC_* keys of a stack.env. splitlines() makes CRLF and LF read the same."""
    out = {}
    for line in text.splitlines():
        key, sep, val = line.strip().partition("=")
        if sep and key.startswith("MIIC_"):
            out[key] = val.strip()
    return out


def rig_name(project: str) -> str:
    """mniis-inv1 -> inv1, cut to fit one row on the device."""
    if project.startswith("mniis-"):
        project = project[len("mniis-"):]
    return project[:RIG_NAME_LEN]


def _port(v: dict, key: str) -> int | None:
    try:
        p = int(v.get(key, ""))
    except ValueError:
        return None
    return p if 0 < p < 65536 else None


def rig_from_env(v: dict) -> tuple[str, tuple[int, ...]] | None:
    project = v.get("MIIC_COMPOSE_PROJECT") or v.get("MIIC_DB_CONTAINER")
    if not project:
        return None
    ports = tuple(p for p in (_port(v, "MIIC_DB_PORT"), _port(v, "MIIC_BACKEND_PORT"),
                              _port(v, "MIIC_FRONTEND_PORT")) if p)
    return rig_name(project), ports


def _worktree_dirs(main_repo: str, bases: tuple[str, ...]) -> list[str]:
    dirs = []
    wt_meta = os.path.join(main_repo, ".git", "worktrees")
    try:
        names = os.listdir(wt_meta)
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(wt_meta, name, "gitdir"), encoding="utf-8") as f:
                gitdir = f.read().strip()
        except OSError:
            continue
        dirs.append(os.path.dirname(os.path.normpath(gitdir)))
    for base in bases:
        try:
            dirs.extend(os.path.join(base, d) for d in os.listdir(base))
        except OSError:
            pass
    return dirs


def discover_rigs(main_repo: str = MAIN_REPO, bases: tuple[str, ...] = RIG_BASES) -> list:
    """[(name, ports)] for the primary stack plus every stack.env, one per name."""
    rigs = {PRIMARY_RIG[0]: PRIMARY_RIG[1]}
    seen = set()
    for d in _worktree_dirs(main_repo, bases):
        key = os.path.normcase(os.path.normpath(d))
        if key in seen:
            continue
        seen.add(key)
        try:
            with open(os.path.join(d, "stack.env"), encoding="utf-8", errors="replace") as f:
                rig = rig_from_env(parse_stack_env(f.read()))
        except OSError:
            continue
        if rig and rig[1] and rig[0] not in rigs:
            rigs[rig[0]] = rig[1]
    return list(rigs.items())


def open_ports(ports, timeout: float = PROBE_TIMEOUT_S) -> set[int]:
    """Ports listening on 127.0.0.1, all connected at once and waited on together.

    Windows retries a refused localhost SYN for about a second instead of
    failing at once, so a closed port costs the full timeout. One select over
    every socket bounds the whole probe to a single timeout (measured: 30
    ports one by one took 1.5 s).
    """
    socks = {}
    found = set()
    try:
        for p in set(ports):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setblocking(False)
            socks[s] = p
            if s.connect_ex(("127.0.0.1", p)) == 0:
                found.add(p)
        pending = [s for s, p in socks.items() if p not in found]
        if pending:
            _, ready, _ = select.select([], pending, pending, timeout)
            for s in ready:
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    found.add(socks[s])
    except OSError:
        pass
    finally:
        for s in socks:
            s.close()
    return found


def rig_state(ports: tuple[int, ...], listening: set[int]) -> int:
    up = sum(1 for p in ports if p in listening)
    if up == 0:
        return RIG_DOWN
    return RIG_UP if up == len(ports) else RIG_PARTIAL


_ORDER = {RIG_UP: 0, RIG_PARTIAL: 1, RIG_DOWN: 2}


def probe_rigs(rigs: list, probe: Callable[[list], set] = open_ports) -> tuple:
    listening = probe([p for _, ports in rigs for p in ports])
    states = [(name, rig_state(ports, listening)) for name, ports in rigs]
    states.sort(key=lambda r: (_ORDER[r[1]], r[0]))
    return tuple(states)


# ---------------------------------------------------------------------------
# Payload / overlay text
# ---------------------------------------------------------------------------

def payload_fields(snap: Snapshot | None) -> dict:
    """Short BLE keys: cpu, rf (free GB), rp (used %), rg ([[name, state], ...])."""
    if snap is None:
        return {}
    return {
        "cpu": snap.cpu,
        "rf": snap.ram_free_gb,
        "rp": snap.ram_used_pct,
        "rg": [[n, s] for n, s in snap.rigs[:MAX_RIGS]],
    }


# ---------------------------------------------------------------------------
# Sampler thread
# ---------------------------------------------------------------------------

class MachineSampler:
    """Samples every `sample_s`; probes rigs every `rig_s`; notifies on change."""

    def __init__(self, sample_s: float = SAMPLE_S, rig_s: float = RIG_S,
                 times=system_times, mem=memory, discover=discover_rigs, probe=probe_rigs):
        self._sample_s = sample_s
        self._rig_s = rig_s
        self._times = times
        self._mem = mem
        self._discover = discover
        self._probe = probe
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[Snapshot], None]] = []
        self._prev_times = None
        self._rigs: tuple = ()
        self._since_rigs = rig_s   # probe on the first tick
        self.snapshot: Snapshot | None = None

    def add_listener(self, fn: Callable[[Snapshot], None]) -> None:
        self._listeners.append(fn)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="machine-stats", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> Snapshot | None:
        """One sample. Returns the new snapshot when a rounded value changed."""
        cur = self._times()
        cpu = None
        if cur is not None and self._prev_times is not None:
            cpu = cpu_pct(self._prev_times, cur)
        self._prev_times = cur
        if self._since_rigs >= self._rig_s:
            self._since_rigs = 0.0
            try:
                self._rigs = self._probe(self._discover())
            except Exception:
                pass   # a bad stack.env or socket error keeps the last rig list
        self._since_rigs += self._sample_s
        mem = self._mem()
        if cpu is None or mem is None:
            return None
        snap = Snapshot(cpu, mem[0], mem[1], self._rigs)
        if snap == self.snapshot:
            return None
        self.snapshot = snap
        for fn in self._listeners:
            try:
                fn(snap)
            except Exception:
                pass
        return snap

    def _run(self) -> None:
        # The first read is only a baseline; take the second after 1 s so the
        # daemon's first write after connect (about 3 s in) already has a value.
        self.tick()
        if not self._stop.wait(min(1.0, self._sample_s)):
            self.tick()
        while not self._stop.wait(self._sample_s):
            self.tick()


_shared: MachineSampler | None = None
_shared_lock = threading.Lock()


def running_sampler() -> MachineSampler | None:
    return _shared


def shared_sampler() -> MachineSampler:
    """The one sampler the daemon loop and the overlay both read, started once."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = MachineSampler()
            _shared.start()
        return _shared
