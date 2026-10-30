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
import struct
import sys
import threading
from typing import Callable, NamedTuple

SAMPLE_S = 5.0
RIG_S = 60.0
PROBE_TIMEOUT_S = 0.05

# Folders rigs.mjs (~/.claude/bin/rigs.mjs) scans. The primary checkout's
# worktrees come from its .git/worktrees/*/gitdir files instead of
# `git worktree list`, so discovery spawns no process. The primary checkout's
# own stack (5432/8080/5173) is not a rig: only stack.env stacks are listed.
MAIN_REPO = r"C:\Users\DannyHearit\RiderProjects\mniis"
RIG_BASES = (r"C:\Users\DannyHearit\RiderProjects", r"C:\wt")
PORT_KEYS = (("db", "MIIC_DB_PORT"), ("api", "MIIC_BACKEND_PORT"), ("web", "MIIC_FRONTEND_PORT"))

MAX_RIGS = 8
RIG_NAME_LEN = 8

RIG_DOWN, RIG_UP, RIG_PARTIAL = 0, 1, 2


class Snapshot(NamedTuple):
    cpu: int                 # 0-100, all cores
    ram_free_gb: float       # one decimal
    ram_used_pct: int        # 0-100
    rigs: tuple              # ((name, state, ((label, port, open), ...)), ...)
                             # up first, then partial, then down


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


def rig_from_env(v: dict) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    """(name, (("db", 5968), ("api", 18968), ("web", 5768))) with absent ports left out."""
    project = v.get("MIIC_COMPOSE_PROJECT") or v.get("MIIC_DB_CONTAINER")
    if not project:
        return None
    ports = tuple((label, p) for label, key in PORT_KEYS if (p := _port(v, key)))
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
    """[(name, ((label, port), ...))] for every stack.env, one per name."""
    rigs = {}
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
    """((name, state, ((label, port, open), ...)), ...) sorted up, partial, down."""
    listening = probe([p for _, ports in rigs for _, p in ports])
    states = [(name, rig_state(tuple(p for _, p in ports), listening),
               tuple((label, p, p in listening) for label, p in ports))
              for name, ports in rigs]
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
        "rg": [[r[0], r[1]] for r in snap.rigs[:MAX_RIGS]],
    }


# ---------------------------------------------------------------------------
# What uses RAM (on demand only)
# ---------------------------------------------------------------------------
# One NtQuerySystemInformation(SystemProcessInformation) snapshot per call, the
# call Task Manager uses: no per-process handle, nothing kept between calls.
# The overlay calls it only while the RAM half of the panel is hovered.

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = -1073741820   # 0xC0000004 as NTSTATUS
# SYSTEM_PROCESS_INFORMATION on 64-bit Windows: NextEntryOffset @0x00,
# WorkingSetPrivateSize @0x08, ImageName.Length @0x38, ImageName.Buffer @0x40,
# UniqueProcessId @0x50.
_SPI = struct.Struct("<I4xQ40xH6xQ8xQ")


def parse_process_info(buf: bytes, base: int) -> list[tuple[str, int]]:
    """[(image name, private working set bytes)] from a SystemProcessInformation buffer.

    `base` is the buffer's address: image names point into the same buffer.
    The idle process (pid 0) has no name and is left out.
    """
    out = []
    off = 0
    while off + _SPI.size <= len(buf):
        nxt, private, name_len, name_ptr, pid = _SPI.unpack_from(buf, off)
        start = name_ptr - base
        if pid and 0 <= start and start + name_len <= len(buf):
            out.append((buf[start:start + name_len].decode("utf-16-le", "replace"), private))
        if not nxt:
            break
        off += nxt
    return out


def top_memory(procs: list[tuple[str, int]], n: int = 6) -> list[tuple[str, int, int]]:
    """[(name, bytes, process count)] grouped by image name, largest first."""
    groups: dict[str, list] = {}
    for name, size in procs:
        if name.lower().endswith(".exe"):
            name = name[:-4]
        g = groups.setdefault(name.lower(), [name, 0, 0])
        g[1] += size
        g[2] += 1
    ranked = sorted(groups.values(), key=lambda g: -g[1])
    return [tuple(g) for g in ranked[:n]]


def process_memory() -> list[tuple[str, int]]:
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        return []
    query = ctypes.windll.ntdll.NtQuerySystemInformation
    query.restype = ctypes.c_long
    size = 1 << 19
    for _ in range(4):
        buf = ctypes.create_string_buffer(size)
        need = ctypes.c_ulong()
        status = query(_SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(need))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = need.value + (1 << 16)
            continue
        if status < 0:
            return []
        return parse_process_info(buf.raw[:need.value], ctypes.addressof(buf))
    return []


def memory_detail(n: int = 6) -> tuple[float, float, list] | None:
    """(used GB, total GB, top_memory list), or None off Windows."""
    if sys.platform != "win32":
        return None
    ms = _MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return None
    used = (ms.ullTotalPhys - ms.ullAvailPhys) / 2**30
    return round(used, 1), round(ms.ullTotalPhys / 2**30, 1), top_memory(process_memory(), n)


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
