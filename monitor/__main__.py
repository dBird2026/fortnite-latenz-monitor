"""Fortnite-Latenz-Monitor.

  python -m monitor            Messdienst starten (läuft dauerhaft, Strg+C beendet)
  python -m monitor einmal     eine einzelne Messrunde (Test)
  python -m monitor status     Übersicht der letzten 24 Stunden und Tagesgang
  python -m monitor dashboard  nur das Dashboard öffnen (ohne Messung)
"""
from __future__ import annotations

import asyncio
import statistics
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import webbrowser

from . import VERSION
from . import config as config_mod
from . import dashboard
from .db import Database
from .service import Monitor



def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%d.%m.%Y %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(config_mod.data_dir() / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def status(db: Database, cfg: config_mod.Config) -> None:
    con = db.con
    print(f"\nDatenbank: {db.path}")
    n, first = con.execute("SELECT COUNT(*), MIN(ts_utc) FROM measurements").fetchone()
    print(f"Messungen gesamt: {n}  (seit {first or '-'})\n")
    print(f"{'Ziel':<24} {'letzte Messung':<17} {'Median':>8} {'p95':>8} {'Verl.':>6} | 24h: {'Median':>7} {'p95':>7} {'>krit.':>7}")
    print("-" * 104)
    rows = con.execute("""
        SELECT t.name, t.id,
          (SELECT ts_utc FROM measurements WHERE target_id=t.id ORDER BY ts_utc DESC LIMIT 1) AS last_ts
        FROM targets t WHERE t.active=1 ORDER BY t.id""").fetchall()
    for r in rows:
        last = con.execute("SELECT * FROM measurements WHERE target_id=? ORDER BY ts_utc DESC LIMIT 1", (r["id"],)).fetchone()
        day = con.execute("SELECT rtt_median, rtt_p95, loss_pct FROM measurements WHERE target_id=? "
                          "AND ts_utc >= datetime('now','-1 day') AND rtt_median IS NOT NULL", (r["id"],)).fetchall()
        f = lambda v: f"{v:7.1f}" if v is not None else "      -"  # noqa: E731
        if day:
            meds = sorted(x[0] for x in day)
            p95s = sorted(x[1] for x in day)
            crit = 100 * sum(1 for x in day if x[0] > cfg.kritisch_ms or x[2] > cfg.verlust_kritisch_pct) / len(day)
            d = f"{f(meds[len(meds) // 2])} {f(p95s[int(0.95 * (len(p95s) - 1))])} {crit:6.0f}%"
        else:
            d = "      -       -       -"
        if last:
            from datetime import datetime
            lt = datetime.fromisoformat(last["ts_utc"]).astimezone().strftime("%d.%m. %H:%M")
            print(f"{r['name']:<24} {lt:<17} {f(last['rtt_median'])} {f(last['rtt_p95'])} {last['loss_pct']:5.0f}% | {d}")
        else:
            print(f"{r['name']:<24} {'-':<17}")

    print("\nTagesgang Fortnite-Server (Median aller Epic-Ziele je Stunde, letzte 14 Tage, Ortszeit):")
    hours = con.execute("""
        SELECT strftime('%H', m.ts_utc, 'localtime') h, m.rtt_median FROM measurements m JOIN targets t ON t.id=m.target_id
        WHERE t.typ='epic' AND m.rtt_median IS NOT NULL AND m.ts_utc >= datetime('now','-14 days')""").fetchall()
    by = {}
    for h, v in hours:
        by.setdefault(h, []).append(v)
    for h in sorted(by):
        med = statistics.median(by[h])
        bar = "#" * min(60, int(med / 4))
        mark = "  <- kritisch" if med > cfg.kritisch_ms else ""
        print(f"  {h}:00  {med:6.1f} ms  {bar}{mark}")
    sp = con.execute("SELECT ts_utc, down_mbit, up_mbit, error FROM speedtests ORDER BY ts_utc DESC LIMIT 3").fetchall()
    if sp:
        print("\nLetzte Speedtests:")
        for s in sp:
            print(f"  {s[0]}  " + (f"{s[1]:.1f} / {s[2]:.1f} Mbit/s" if s[1] is not None else f"Fehler: {s[3]}"))
    ev = con.execute("SELECT ts_utc, typ, text FROM events ORDER BY id DESC LIMIT 5").fetchall()
    if ev:
        print("\nLetzte Ereignisse:")
        for e in ev:
            print(f"  {e[0]}  {e[1]:<8} {e[2]}")
    print()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows-Konsole: nie an Sonderzeichen scheitern
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    cfg, cfg_path = config_mod.load()
    db = Database(config_mod.data_dir() / "data.db")
    if cmd == "status":
        status(db, cfg)
        return
    setup_logging()
    log = logging.getLogger("monitor")
    log.info("Fortnite-Latenz-Monitor %s | Konfiguration: %s", VERSION, cfg_path)
    url = f"http://localhost:{cfg.dashboard_port}"
    if cmd == "dashboard":
        srv = dashboard.start(db.path, cfg, cfg.dashboard_port)
        webbrowser.open(url)
        if srv is None:  # läuft bereits im Messprogramm
            return
        log.info("Dashboard läuft ohne Messung. Fenster schließen oder Strg+C beendet es.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return
    mon = Monitor(cfg, db)
    if cmd != "einmal":
        if dashboard.start(db.path, cfg, cfg.dashboard_port) and cfg.browser_oeffnen:
            webbrowser.open(url)
    try:
        if cmd == "einmal":
            mon.resolve_targets()
            asyncio.run(mon.run_round())
        else:
            asyncio.run(mon.run_forever())
    except KeyboardInterrupt:
        log.info("Beendet.")


if __name__ == "__main__":
    main()
