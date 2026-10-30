#!/usr/bin/env python3
"""Unit tests for the machine meter sampler (machine_stats.py).

Kernel counters, discovery and port probes are injected, so these run on any
OS without touching the machine or the network.

Run: python -m pytest daemon/tests/test_machine_stats.py -x -q
"""
import socket
import struct

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
    assert m.rig_from_env(m.parse_stack_env(STACK_ENV)) == (
        "inv1", (("db", 5968), ("api", 18968), ("web", 5768)))
    db_only = m.parse_stack_env("MIIC_COMPOSE_PROJECT=mniis-1751\r\nMIIC_DB_PORT=5751\r\n")
    assert m.rig_from_env(db_only) == ("1751", (("db", 5751),))


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
    assert rigs["a"] == (("db", 6001),)
    assert rigs["b"] in ((("db", 6002),), (("db", 6003),))   # one entry per name
    assert len(rigs) == 2   # the primary checkout's own stack is not a rig


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
    rigs = [("z", (("db", 1),)), ("down", (("db", 9),)),
            ("part", (("db", 1), ("api", 9))), ("a", (("db", 2),))]
    out = m.probe_rigs(rigs, probe=lambda ports: {1, 2})
    assert [r[:2] for r in out] == [("a", 1), ("z", 1), ("part", 2), ("down", 0)]
    assert out[2][2] == (("db", 1, True), ("api", 9, False))


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
                         discover=lambda: [("inv1", (("db", 1),))], probe=probe)
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


def test_payload_fields_sends_name_and_state_only():
    snap = m.Snapshot(1, 2.0, 3, (("inv1", 1, (("db", 1, True),)),))
    assert m.payload_fields(snap)["rg"] == [["inv1", 1]]


# ---------------------------------------------------------------------------
# What uses RAM
# ---------------------------------------------------------------------------

def _spi_entry(next_off, private, name_len, name_ptr, pid, size=256):
    head = m._SPI.pack(next_off, private, name_len, name_ptr, pid)
    return head + b"\0" * (size - len(head))


def test_parse_process_info_reads_names_from_the_same_buffer():
    base = 0x10000
    names = "chrome.exe".encode("utf-16-le") + "Code.exe".encode("utf-16-le")
    idle = _spi_entry(256, 0, 0, 0, 0)
    chrome = _spi_entry(256, 3 << 20, 20, base + 768, 1234)
    code = _spi_entry(0, 5 << 20, 16, base + 788, 99)
    buf = idle + chrome + code + names
    assert m.parse_process_info(buf, base) == [("chrome.exe", 3 << 20), ("Code.exe", 5 << 20)]


def test_parse_process_info_skips_names_outside_the_buffer():
    buf = _spi_entry(0, 1, 8, 0xDEAD0000, 7)
    assert m.parse_process_info(buf, 0x1000) == []


def test_top_memory_groups_by_name_and_ranks():
    procs = [("chrome.exe", 300), ("Code.exe", 500), ("chrome.exe", 400), ("System", 10)]
    assert m.top_memory(procs, 2) == [("chrome", 700, 2), ("Code", 500, 1)]


def test_memory_detail_live_on_windows():
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows only")
    used, total, top = m.memory_detail()
    assert 0 < used < total
    assert top and all(size > 0 for _, size, _ in top)


# ---------------------------------------------------------------------------
# BLE payload piggyback (claude_usage_daemon_windows.machine_extra / Session)
# ---------------------------------------------------------------------------

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from daemon import claude_usage_daemon_windows as d


@pytest.fixture(autouse=True)
def _quiet_daemon_log(monkeypatch):
    """Keep Session.write_payload out of the real %LOCALAPPDATA% daemon.log."""
    monkeypatch.setattr(d, "log", lambda _msg: None)

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


# ---------------------------------------------------------------------------
# Overlay pure helpers (overlay_windows.py)
# ---------------------------------------------------------------------------

from daemon import overlay_windows as ov


def test_rig_line_names_up_and_partial_and_counts_down():
    rigs = (("inv1", 1), ("g1", 2, (("db", 1, True),)), ("ci1", 0), ("t1", 0))
    assert ov.rig_line(rigs) == "inv1 up \u00b7 g1 partial \u00b7 2 down"


def test_rig_line_empty():
    assert ov.rig_line(()) == "No rigs found"


def test_overlay_content_equal_for_equal_snapshots():
    a = m.Snapshot(37, 14.2, 56, (("inv1", 1),))
    assert ov.content(a) == ov.content(m.Snapshot(37, 14.2, 56, (("inv1", 1),)))
    assert ov.content(a) != ov.content(a._replace(cpu=38))
    assert ov.content(a)[0] == "CPU 37%" and ov.content(a)[2] == "RAM 14.2 GB free"
    assert ov.content(None) is None


def test_overlay_content_changes_with_the_open_card():
    a = m.Snapshot(37, 14.2, 56, (("inv1", 1),))
    assert ov.content(a) != ov.content(a, (("title", "x"),))


def test_rig_card_lists_every_rig_with_ports():
    rigs = (("inv1", 1, (("db", 1, True), ("api", 2, True))), ("ci1", 0, (("db", 3, False),)))
    card = ov.rig_card(rigs)
    assert card[0][0] == "title"
    assert card[1] == ("rig", "inv1", 1, (("db", 1, True), ("api", 2, True)))
    assert card[2] == ("rig", "ci1", 0, (("db", 3, False),))
    assert ov.rig_card(())[0] == ("title", "No stack.env rigs found")


def test_rig_card_caps_rows():
    rigs = tuple((f"r{i}", 0, ()) for i in range(ov.MAX_CARD_RIGS + 3))
    card = ov.rig_card(rigs)
    assert len(card) == ov.MAX_CARD_RIGS + 2
    assert card[-1] == ("more", "+3 more")


def test_ram_card_rows():
    card = ov.ram_card((21.8, 31.5, [("claude", 4 << 30, 30), ("MsMpEng", 443 << 20, 1)]))
    assert card[0] == ("title", "In use 21.8 of 31.5 GB \u00b7 largest apps")
    assert card[1] == ("proc", "claude (30)", "4.0 GB")
    assert card[2] == ("proc", "MsMpEng", "443 MB")
    assert ov.ram_card(None)[0][0] == "title"


def test_hover_region_maps_panel_parts():
    # window 280x150 at (1000, 500), panel is the bottom 50 px, top row 27 px
    rect, size = (1000, 500, 1280, 650), (280, 150)
    hit = lambda x, y: ov.hover_region((x, y), rect, size, 50, 27)
    assert hit(999, 640) is None and hit(1100, 650) is None
    assert hit(1100, 550) == "card"
    assert hit(1050, 610) == "cpu"
    assert hit(1200, 610) == "ram"
    assert hit(1200, 640) == "rigs"


def test_hover_region_scales_when_rect_and_bitmap_differ():
    # a DPI-virtualized rect at half the bitmap size still maps to the same parts
    hit = lambda x, y: ov.hover_region((x, y), (0, 0, 140, 25), (280, 50), 50, 27)
    assert hit(100, 5) == "ram" and hit(10, 5) == "cpu" and hit(10, 20) == "rigs"


def test_overlay_pct_color_thresholds():
    assert ov.pct_color(49) == ov.COL_GREEN
    assert ov.pct_color(50) == ov.COL_AMBER
    assert ov.pct_color(80) == ov.COL_RED


def test_overlay_setting_round_trip_keeps_other_lines(tmp_path):
    cfg = tmp_path / "Clawdmeter" / "config"
    assert ov.read_setting(cfg) is False
    ov.write_setting(cfg, True)
    assert ov.read_setting(cfg) is True
    cfg.write_text("chime = on\noverlay = on  # desk\nclock = 24\n")
    ov.write_setting(cfg, False)
    assert cfg.read_text() == "chime = on\noverlay = off\nclock = 24\n"
    assert ov.read_setting(cfg) is False
