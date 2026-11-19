"""Grundtests: Paketformat, Statistik, Traceroute-Auswertung, Datenbank + Dashboard-API."""
import asyncio
import json
import socket
import threading
import urllib.request

import pytest

from monitor import config as config_mod
from monitor import dashboard, probes
from monitor.db import Database, now_utc


def test_epic_packet_format():
    pkt = probes.epic_packet(0x0102)
    assert len(pkt) == 22
    assert pkt[:4] == bytes([0x01, 0x02, 0x01, 0x01])
    assert pkt[6:8] == b"\x00\x00"
    assert pkt[14:] == b"\xaa" * 4 + b"\xbb" * 4


def test_stats():
    r = probes.Result(5, [10.0, 20.0, None, 30.0, 40.0])
    st = r.stats()
    assert st["received"] == 4 and st["loss_pct"] == pytest.approx(20.0)
    assert st["rtt_min"] == 10.0 and st["rtt_max"] == 40.0 and st["rtt_median"] == 25.0
    assert st["jitter"] == pytest.approx(10.0)
    empty = probes.Result(3, [None] * 3).stats()
    assert empty["received"] == 0 and empty["rtt_median"] is None


@pytest.mark.parametrize("out,expected", [
    ("  1    <1 ms    <1 ms    <1 ms  192.168.168.1\r\n  2    25 ms    47 ms   108 ms  100.127.224.2\r\n"
     "  3     *        *        *     Zeitüberschreitung der Anforderung.\r\n", ["192.168.168.1", "100.127.224.2"]),
    ("traceroute to 1.1.1.1 (1.1.1.1), 4 hops max\n 1  192.168.168.1  0.512 ms\n 2  100.127.224.2  21.3 ms\n 3  *\n"
     " 4  1.1.1.1  30.1 ms\n", ["192.168.168.1", "100.127.224.2"]),
])
def test_parse_trace(out, expected):
    assert probes.parse_trace(out) == expected


def test_probe_epic_against_local_echo():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    stop = threading.Event()

    def echo():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                data, addr = srv.recvfrom(100)
                srv.sendto(data, addr)
            except OSError:
                pass
    t = threading.Thread(target=echo, daemon=True)
    t.start()
    try:
        res = asyncio.run(probes.probe_epic("127.0.0.1", port, 5, 10, 500))
    finally:
        stop.set()
        t.join(1)
        srv.close()
    assert res.stats()["received"] == 5


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FLM_BROWSER", "0")
    cfg, _ = config_mod.load()
    db = Database(tmp_path / "data.db")
    return cfg, db


def test_config_defaults(env):
    cfg, _ = env
    assert cfg.intervall_min == 5 and not cfg.browser_oeffnen
    assert any(t.typ == "epic" for t in cfg.ziele)


def test_dashboard_api(env):
    cfg, db = env
    tid = db.upsert_target("Fortnite Test", "epic", "127.0.0.1", "127.0.0.1", 22222)
    st = probes.Result(4, [30.0, 35.0, 40.0, None]).stats()
    db.add_measurement(tid, now_utc(), st, [30.0, 35.0, 40.0, None])
    db.add_speedtest(now_utc(), {"down_mbit": 0, "up_mbit": 0, "ping_ms": 1, "server": "x"})  # ungültig -> ausgeblendet
    db.add_speedtest(now_utc(), {"down_mbit": 80, "up_mbit": 30, "ping_ms": 1, "server": "x"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = dashboard.start(db.path, cfg, port)
    try:
        base = f"http://127.0.0.1:{port}"
        status = json.load(urllib.request.urlopen(base + "/api/status"))
        assert status["fortnite_median"] == pytest.approx(35.0)
        assert status["speedtest"]["down_mbit"] == 80
        serie = json.load(urllib.request.urlopen(base + "/api/serie?stunden=24"))
        assert [p[1] for p in serie["speedtests"]] == [80]
        assert urllib.request.urlopen(base + "/").status == 200
        csv = urllib.request.urlopen(base + "/api/export.csv?stunden=24").read().decode("utf-8-sig")
        assert "Fortnite Test" in csv and "35,0" in csv
    finally:
        srv.shutdown()
