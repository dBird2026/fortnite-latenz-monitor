"""Messdienst: Runde alle N Minuten, stündlicher Speedtest, Lücken- und Zielüberwachung."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from datetime import datetime, timezone

from . import probes
from .config import Config
from .db import Database, now_utc

log = logging.getLogger(__name__)


def keep_awake(on: bool) -> None:
    """Standby verhindern (Windows), Bildschirm darf aus."""
    if sys.platform != "win32":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Monitor:
    def __init__(self, cfg: Config, db: Database):
        self.cfg, self.db = cfg, db
        self.targets: list[tuple] = []  # (target_id, Target, aufgelöste Adresse)
        self._speedtest_task: asyncio.Task | None = None

    # -------------------------------------------------------------- Ziele
    def resolve_targets(self) -> None:
        hops = []
        if any(t.host.startswith("auto-") for t in self.cfg.ziele):
            hops = probes.trace_first_hops()
            log.info("Erste Hops: %s", hops or "keine")
        auto = {"auto-router": hops[0] if len(hops) > 0 else None,
                "auto-provider": hops[1] if len(hops) > 1 else None}
        self.targets = []
        self.missing_auto = False
        for t in self.cfg.ziele:
            resolved = auto.get(t.host, t.host)
            tid = self.db.upsert_target(t.name, t.typ, t.host, resolved, t.port)
            if resolved:
                self.targets.append((tid, t, resolved))
                log.info("Ziel %-24s %-5s %s", t.name, t.typ, resolved)
            else:
                self.missing_auto = True
                log.warning("Ziel %s (%s) konnte nicht ermittelt werden", t.name, t.host)
                self.db.add_event("Warnung", f"Ziel {t.name} ({t.host}) nicht ermittelbar")
        self.db.deactivate_others([t.name for t in self.cfg.ziele])

    # -------------------------------------------------------------- Messrunde
    async def run_round(self) -> None:
        c = self.cfg
        ts = now_utc()
        for tid, t, host in self.targets:  # nacheinander, damit sich die Messungen nicht gegenseitig stören
            if t.typ == "epic":
                res = await probes.probe_epic(host, t.port, c.pakete, c.abstand_ms, c.timeout_ms)
            else:
                res = await probes.probe_icmp(host, c.pakete, c.abstand_ms, c.timeout_ms)
            st = res.stats()
            self.db.add_measurement(tid, ts, st, res.rtts, res.error)
            log.info("%-24s %s", t.name,
                     f"Median {st['rtt_median']:.1f} ms | p95 {st['rtt_p95']:.1f} | Verlust {st['loss_pct']:.0f} %"
                     if st["received"] else f"keine Antwort {res.error}")
            if t.typ == "epic":
                last = self.db.recent_loss(tid, 12)
                if len(last) == 12 and all(x >= 100 for x in last):
                    self.db.add_event("Warnung", f"{t.name} ({host}) antwortet seit 1 Stunde nicht - "
                                                 "evtl. neue Server-IPs, Mitschnitt wiederholen")

    async def maybe_speedtest(self) -> None:
        c = self.cfg
        if not c.speedtest_aktiv or (self._speedtest_task and not self._speedtest_task.done()):
            return
        last = _parse(self.db.last_speedtest_ts())
        if last and (datetime.now(timezone.utc) - last).total_seconds() < c.speedtest_intervall_min * 60 - 30:
            return

        async def _run():
            ts = now_utc()
            try:
                res = await asyncio.to_thread(probes.run_speedtest)
                self.db.add_speedtest(ts, res)
                log.info("Speedtest: %.1f / %.1f Mbit/s (%s)", res["down_mbit"], res["up_mbit"], res["server"])
            except Exception as e:  # noqa: BLE001
                self.db.add_speedtest(ts, None, str(e)[:300])
                log.warning("Speedtest fehlgeschlagen: %s", e)
        self._speedtest_task = asyncio.create_task(_run())

    # -------------------------------------------------------------- Hauptschleife
    async def run_forever(self) -> None:
        c = self.cfg
        interval = c.intervall_min * 60
        last = _parse(self.db.last_ts())
        if last and (gap := (datetime.now(timezone.utc) - last).total_seconds()) > 2 * interval:
            self.db.add_event("Lücke", f"Keine Messung seit {last.astimezone():%d.%m.%Y %H:%M} "
                                       f"({gap / 60:.0f} Min) - Programm war nicht aktiv", last.isoformat())
        self.db.add_event("Start", "Messdienst gestartet")
        self.resolve_targets()
        keep_awake(c.wach_halten)
        prev = None
        rounds = 0
        try:
            while True:
                wall = datetime.now(timezone.utc)
                if prev and (wall - prev).total_seconds() > 2 * interval:
                    self.db.add_event("Lücke", f"Messpause von {(wall - prev).total_seconds() / 60:.0f} Min "
                                               "(Standby oder Netzwerk weg)", prev.isoformat())
                    self.resolve_targets()
                prev = wall
                if getattr(self, "missing_auto", False) and rounds and rounds % 12 == 0:
                    self.resolve_targets()  # z. B. Netzwerk war beim Start noch nicht bereit
                try:
                    await self.run_round()
                    # Speedtest erst nach der Latenzrunde, damit er die Messung nicht verfälscht
                    await self.maybe_speedtest()
                except Exception:  # noqa: BLE001
                    log.exception("Fehler in der Messrunde")
                rounds += 1
                if rounds % 288 == 1:
                    self.db.housekeeping()
                    self.db.daily_backup()
                # nächste Runde am Intervall-Raster (z. B. :00, :05, :10 ...)
                now = datetime.now()
                secs = interval - ((now.minute * 60 + now.second) % interval)
                await asyncio.sleep(max(5.0, secs))
        finally:
            keep_awake(False)
            if self._speedtest_task and not self._speedtest_task.done():
                self._speedtest_task.cancel()
            self.db.add_event("Stopp", "Messdienst beendet")
