"""Messungen: Epic-QoS (UDP 22222), ICMP-Ping, Pfad-Ermittlung, Speedtest."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import statistics
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAGIC = b"\xaa" * 4 + b"\xbb" * 4


@dataclass
class Result:
    sent: int
    rtts: list[float | None] = field(default_factory=list)  # je Paket in Sendereihenfolge, None = verloren
    error: str = ""

    @property
    def ok(self) -> list[float]:
        return [r for r in self.rtts if r is not None]

    def stats(self) -> dict:
        ok = self.ok
        received = len(ok)
        d = {"sent": self.sent, "received": received,
             "loss_pct": 100.0 * (self.sent - received) / self.sent if self.sent else 100.0,
             "rtt_min": None, "rtt_median": None, "rtt_avg": None, "rtt_p95": None, "rtt_max": None, "jitter": None}
        if ok:
            s = sorted(ok)
            d.update(rtt_min=s[0], rtt_median=statistics.median(s), rtt_avg=statistics.fmean(s),
                     rtt_p95=s[min(len(s) - 1, max(0, round(0.95 * len(s) + 0.4999) - 1))], rtt_max=s[-1])
            d["jitter"] = statistics.fmean(abs(b - a) for a, b in zip(ok, ok[1:])) if len(ok) > 1 else 0.0
        return d


# ------------------------------------------------------------------ Epic-QoS
def epic_packet(seq: int) -> bytes:
    """22-Byte-Paket wie von Fortnite gesendet (Mitschnitt 08.11.2026)."""
    ts48 = (time.perf_counter_ns() // 100) & 0xFFFFFFFFFFFF
    return struct.pack(">HHHH", seq & 0xFFFF, (seq - 1) & 0xFFFF, random.getrandbits(16), 0) + ts48.to_bytes(6, "big") + MAGIC


class _EpicProtocol(asyncio.DatagramProtocol):
    def __init__(self, pending: dict[bytes, float], rtts: dict[bytes, float]):
        self.pending, self.rtts = pending, rtts

    def datagram_received(self, data: bytes, addr) -> None:
        t = time.perf_counter()
        if data in self.pending and data not in self.rtts:
            self.rtts[data] = (t - self.pending[data]) * 1000.0

    def error_received(self, exc) -> None:  # z. B. ICMP unreachable
        log.debug("UDP-Fehler: %s", exc)


async def probe_epic(host: str, port: int, count: int, gap_ms: int, timeout_ms: int) -> Result:
    loop = asyncio.get_running_loop()
    pending: dict[bytes, float] = {}
    rtts: dict[bytes, float] = {}
    order: list[bytes] = []
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: _EpicProtocol(pending, rtts), remote_addr=(host, port))
    except OSError as e:
        return Result(count, [None] * count, f"Socket: {e}")
    try:
        base = random.randrange(1, 60000)
        for i in range(count):
            pkt = epic_packet(base + i)
            order.append(pkt)
            pending[pkt] = time.perf_counter()
            transport.sendto(pkt)
            await asyncio.sleep(gap_ms / 1000)
        deadline = time.perf_counter() + timeout_ms / 1000
        while len(rtts) < count and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
    finally:
        transport.close()
    return Result(count, [rtts.get(p) for p in order])


# ------------------------------------------------------------------ ICMP
def _icmp_windows(host: str, count: int, gap_ms: int, timeout_ms: int) -> Result:
    """ICMP über die Windows-API IcmpSendEcho - braucht keine Administratorrechte."""
    import ctypes
    import socket
    from ctypes import wintypes

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                    ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]

    iphlp = ctypes.windll.iphlpapi
    iphlp.IcmpCreateFile.restype = wintypes.HANDLE
    iphlp.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    addr = struct.unpack("<L", socket.inet_aton(socket.gethostbyname(host)))[0]
    payload = b"FortniteLatenzMonitor-----------"  # 32 Byte wie Windows-ping
    buf_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8 + 64
    handle = iphlp.IcmpCreateFile()
    rtts: list[float | None] = []
    try:
        for i in range(count):
            buf = ctypes.create_string_buffer(buf_size)
            n = iphlp.IcmpSendEcho(handle, addr, payload, len(payload), None, buf, buf_size, timeout_ms)
            reply = ICMP_ECHO_REPLY.from_buffer(buf)
            rtts.append(float(reply.RoundTripTime) if n > 0 and reply.Status == 0 else None)
            if i < count - 1:
                time.sleep(gap_ms / 1000)
    finally:
        iphlp.IcmpCloseHandle(handle)
    return Result(count, rtts)


async def probe_icmp(host: str, count: int, gap_ms: int, timeout_ms: int) -> Result:
    try:
        if sys.platform == "win32":
            return await asyncio.to_thread(_icmp_windows, host, count, gap_ms, timeout_ms)
        import os
        from icmplib import async_ping
        r = await async_ping(host, count=count, interval=gap_ms / 1000, timeout=timeout_ms / 1000,
                             privileged=(os.geteuid() == 0))
        rt = list(r.rtts) + [None] * (count - len(r.rtts))  # icmplib liefert nur erfolgreiche RTTs
        return Result(count, rt)
    except Exception as e:  # noqa: BLE001
        return Result(count, [None] * count, f"ICMP: {e}")


# ------------------------------------------------------------------ Pfad
def trace_first_hops(target: str = "1.1.1.1", max_hops: int = 4) -> list[str]:
    """Liefert die IPs der ersten Hops (Router, Provider-Knoten, ...)."""
    if sys.platform == "win32":
        cmd = ["tracert", "-d", "-h", str(max_hops), "-w", "1000", target]
        flags = subprocess.CREATE_NO_WINDOW
    else:
        cmd = ["traceroute", "-n", "-m", str(max_hops), "-w", "1", "-q", "1", target]
        flags = 0
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=flags).stdout.decode(errors="ignore")
    except Exception as e:  # noqa: BLE001
        log.warning("traceroute fehlgeschlagen: %s", e)
        return []
    return parse_trace(out, target)


def parse_trace(out: str, target: str = "1.1.1.1") -> list[str]:
    """Hop-IPs aus tracert (Windows) oder traceroute -n (Linux) lesen."""
    hops = []
    for line in out.splitlines():
        if not re.match(r"^\s*\d+\s", line):
            continue
        m = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", line)
        if m and m.group(1) != target:
            hops.append(m.group(1))
    return hops


# ------------------------------------------------------------------ Speedtest
def run_speedtest() -> dict:
    """Down-/Upload in Mbit/s über speedtest.net (Paket speedtest-cli)."""
    import speedtest
    st = speedtest.Speedtest(secure=True)
    best = st.get_best_server()
    down = st.download() / 1e6
    up = st.upload(pre_allocate=False) / 1e6
    if down < 0.1 or up < 0.1:  # z. B. direkt nach dem Hochfahren noch keine Verbindung
        raise RuntimeError(f"Kein gültiges Ergebnis (Down {down:.1f} / Up {up:.1f} Mbit/s)")
    return {"down_mbit": round(down, 1), "up_mbit": round(up, 1), "ping_ms": round(st.results.ping, 1),
            "server": f"{best.get('sponsor', '')} ({best.get('name', '')})"}
