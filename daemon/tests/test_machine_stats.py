#!/usr/bin/env python3
"""Unit tests for the machine meter sampler (machine_stats.py).

Kernel counters, discovery and port probes are injected, so these run on any
OS without touching the machine or the network.

Run: python -m pytest daemon/tests/test_machine_stats.py -x -q
"""
import socket

from daemon import machine_stats as m


# ---------------------------------------------------------------------------
# CPU / RAM arithmetic
# ---------------------------------------------------------------------------

def test_cpu_pct_from_two_reads():
    # (idle, kernel, user); kernel time includes idle time
    prev = (0, 0, 0)
    cur = (250, 600, 400)   # total 1000, idle 250 -> 75% busy
    assert m.cpu_pct(prev, cur) == 75


def test_cpu_pct_zero_elapsed_is_zero():
    assert m.cpu_pct((5, 5, 5), (5, 5, 5)) == 0


def test_cpu_pct_clamped():
    assert m.cpu_pct((0, 0, 0), (0, 100, 0)) == 100


def test_ram_figures_free_gb_one_decimal_and_used_pct():
    total = 32 * 2**30
    avail = int(8.04 * 2**30)
    assert m.ram_figures(avail, total) == (8.0, 75)


def test_ram_figures_zero_total():
    assert m.ram_figures(0, 0) == (0.0, 0)


# ---------------------------------------------------------------------------
# stack.env parsing
# ---------------------------------------------------------------------------

STACK_ENV = (
    "# rig\n"
    "MIIC_DB_PORT=5968\n"
    "MIIC_DB_CONTAINER=mniis-pg-inv1\n"
    "MIIC_COMPOSE_PROJECT=mniis-inv1\n"
    "MIIC_BACKEND_PORT=18968\n"
    "MIIC_FRONTEND_PORT=5768\n"
    "OTHER=1\n"
)


def test_parse_stack_env_lf():
    v = m.parse_stack_env(STACK_ENV)
    assert v["MIIC_DB_PORT"] == "5968"
    assert "OTHER" not in v


def test_parse_stack_env_crlf_reads_like_lf():
    assert m.parse_stack_env(STACK_ENV.replace("\n", "\r\n")) == m.parse_stack_env(STACK_ENV)


def test_rig_from_env_strips_prefix_and_keeps_present_ports():
    assert m.rig_from_env(m.parse_stack_env(STACK_ENV)) == ("inv1", (5968, 18968, 5768))
    db_only = m.parse_stack_env("MIIC_COMPOSE_PROJECT=mniis-1751\r\nMIIC_DB_PORT=5751\r\n")
    assert m.rig_from_env(db_only) == ("1751", (5751,))


def test_rig_from_env_falls_back_to_container_and_ignores_bad_ports():
    v = m.parse_stack_env("MIIC_DB_CONTAINER=mniis-pg-x\nMIIC_DB_PORT=abc\nMIIC_BACKEND_PORT=99999\n")
    assert m.rig_from_env(v) == ("pg-x", ())


def test_rig_from_env_without_project_is_none():
    assert m.rig_from_env({"MIIC_DB_PORT": "1"}) is None


def test_rig_name_is_cut():
    assert m.rig_name("mniis-very-long-name") == "very-lon"


def test_discover_rigs_reads_worktrees_and_bases(tmp_path):
    main = tmp_path / "mniis"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt1 = tmp_path / "elsewhere" / "wt1"
    wt1.mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt1" / "gitdir").write_text(str(wt1 / ".git") + "\n")
    (wt1 / "stack.env").write_text("MIIC_COMPOSE_PROJECT=mniis-a\r\nMIIC_DB_PORT=6001\r\n")
    base = tmp_path / "wt"
    (base / "b").mkdir(parents=True)
    (base / "b" / "stack.env").write_text("MIIC_COMPOSE_PROJECT=mniis-b\nMIIC_DB_PORT=6002\n")
    (base / "dup").mkdir()
    (base / "dup" / "stack.env").write_text("MIIC_COMPOSE_PROJECT=mniis-b\nMIIC_DB_PORT=6003\n")
    (base / "noenv").mkdir()
    rigs = dict(m.discover_rigs(str(main), (str(base), str(tmp_path / "missing"))))
    assert rigs["main"] == (5432, 8080, 5173)
    assert rigs["a"] == (6001,)
    assert rigs["b"] in ((6002,), (6003,))   # one entry per name
    assert len(rigs) == 3


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def test_rig_state_up_partial_down():
    listening = {1, 2}
    assert m.rig_state((1, 2), listening) == m.RIG_UP
    assert m.rig_state((1, 3), listening) == m.RIG_PARTIAL
    assert m.rig_state((3, 4), listening) == m.RIG_DOWN
    assert m.rig_state((), listening) == m.RIG_DOWN


def test_probe_rigs_sorts_up_partial_down():
    rigs = [("z", (1,)), ("down", (9,)), ("part", (1, 9)), ("a", (2,))]
    out = m.probe_rigs(rigs, probe=lambda ports: {1, 2})
    assert out == (("a", 1), ("z", 1), ("part", 2), ("down", 0))


def test_open_ports_sees_a_real_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        closed = free.getsockname()[1]
        free.close()
        assert m.open_ports([port, closed]) == {port}
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, reads):
        self.reads = list(reads)

    def __call__(self):
        return self.reads.pop(0)


def _sampler(times, mem=lambda: (8.0, 75), rigs=(("inv1", 1),), rig_s=60.0):
    calls = {"probe": 0}

    def probe(_found):
        calls["probe"] += 1
        return rigs

    s = m.MachineSampler(sample_s=5.0, rig_s=rig_s, times=_Clock(times), mem=mem,
                         discover=lambda: [("inv1", (1,))], probe=probe)
    return s, calls


def test_first_tick_is_baseline_only():
    s, _ = _sampler([(0, 0, 0)])
    assert s.tick() is None
    assert s.snapshot is None


def test_second_tick_builds_snapshot_and_notifies():
    s, _ = _sampler([(0, 0, 0), (50, 100, 0)])
    seen = []
    s.add_listener(seen.append)
    s.tick()
    snap = s.tick()
    assert snap == m.Snapshot(50, 8.0, 75, (("inv1", 1),))
    assert seen == [snap]


def test_unchanged_rounded_values_do_not_notify():
    s, _ = _sampler([(0, 0, 0), (50, 100, 0), (100, 200, 0)])
    seen = []
    s.add_listener(seen.append)
    s.tick(); s.tick()
    assert s.tick() is None   # still 50%
    assert len(seen) == 1


def test_rigs_probed_on_first_tick_then_every_rig_interval():
    reads = [(i * 10, i * 100, 0) for i in range(14)]
    s, calls = _sampler(reads, rig_s=60.0)
    for _ in range(12):
        s.tick()
    assert calls["probe"] == 1
    s.tick()
    assert calls["probe"] == 2


def test_listener_error_does_not_stop_sampler():
    s, _ = _sampler([(0, 0, 0), (50, 100, 0)])
    s.add_listener(lambda _snap: 1 / 0)
    s.tick()
    assert s.tick() is not None


def test_payload_fields_shape_and_cap():
    rigs = tuple((f"r{i}", 0) for i in range(12))
    out = m.payload_fields(m.Snapshot(37, 14.2, 56, rigs))
    assert out["cpu"] == 37 and out["rf"] == 14.2 and out["rp"] == 56
    assert len(out["rg"]) == m.MAX_RIGS
    assert out["rg"][0] == ["r0", 0]


def test_payload_fields_none_is_empty():
    assert m.payload_fields(None) == {}


# ---------------------------------------------------------------------------
# BLE payload piggyback (claude_usage_daemon_windows.machine_extra / Session)
# ---------------------------------------------------------------------------

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from daemon import claude_usage_daemon_windows as d

USAGE = {"s": 42, "sr": 180, "w": 17, "wr": 8820, "st": "allowed", "acct": "pro", "ok": True}
FIELDS = {"cpu": 37, "rf": 14.2, "rp": 56, "rg": [["inv1", 1], ["s9", 0]]}


def test_machine_extra_first_write_attaches():
    assert d.machine_extra(USAGE, FIELDS, None) == FIELDS


def test_machine_extra_unchanged_attaches_nothing():
    assert d.machine_extra(USAGE, FIELDS, dict(FIELDS)) is None


def test_machine_extra_no_sample_attaches_nothing():
    assert d.machine_extra(USAGE, {}, None) is None


def test_machine_extra_trims_rigs_to_fit_one_write():
    many = dict(FIELDS, rg=[[f"rig{i:013d}", 0] for i in range(8)])
    extra = d.machine_extra(USAGE, many, None)
    size = len(json.dumps({**USAGE, **extra}, separators=(",", ":")))
    assert size <= d.MAX_PAYLOAD_BYTES
    assert 0 < len(extra["rg"]) < 8
    assert extra["rg"] == many["rg"][:len(extra["rg"])]   # cut from the end
    assert len(many["rg"]) == 8                            # input untouched


def _session(fields):
    client = MagicMock()
    client.write_gatt_char = AsyncMock()
    return d.Session(client, machine=lambda: fields), client


def _sent(client):
    return json.loads(client.write_gatt_char.call_args.args[1])


def test_session_sends_machine_keys_once_per_change():
    current = dict(FIELDS)
    s, client = _session(None)
    s._machine = lambda: current
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(s.write_payload(dict(USAGE)))
        assert _sent(client)["cpu"] == 37
        loop.run_until_complete(s.write_payload(dict(USAGE)))
        assert "cpu" not in _sent(client)
        current = dict(FIELDS, cpu=38)
        loop.run_until_complete(s.write_payload({"ok": False}))
        assert _sent(client)["cpu"] == 38 and _sent(client)["ok"] is False
    finally:
        loop.close()


def test_session_failed_write_resends_next_time():
    s, client = _session(dict(FIELDS))
    client.write_gatt_char.side_effect = [OSError("gone"), None]
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(s.write_payload(dict(USAGE))) is False
        assert loop.run_until_complete(s.write_payload(dict(USAGE))) is True
        assert _sent(client)["cpu"] == 37
    finally:
        loop.close()


def test_session_without_sampler_sends_usage_unchanged():
    s, client = _session({})
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(s.write_payload(dict(USAGE)))
    finally:
        loop.close()
    assert _sent(client) == USAGE
