"""Lokales Dashboard: kleiner HTTP-Server (nur 127.0.0.1) mit JSON-API und einer Seite."""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp())


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * len(s) + 0.4999) - 1))]


class Api:
    def __init__(self, db_path: Path, cfg: Config):
        self.db_path, self.cfg = db_path, cfg

    def con(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------ Hilfen
    def _range(self, q: dict) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        to = datetime.fromisoformat(q["bis"][0]) if "bis" in q else now
        frm = datetime.fromisoformat(q["von"][0]) if "von" in q else to - timedelta(hours=float(q.get("stunden", ["24"])[0]))
        if frm.tzinfo is None:
            frm = frm.astimezone()
        if to.tzinfo is None:
            to = to.astimezone()
        return frm, to

    def _rounds(self, c, frm: datetime, to: datetime) -> list[dict]:
        """Eine Zeile je Messrunde: Mittel über alle Fortnite-Server."""
        rows = c.execute("""
            SELECT m.ts_utc, AVG(m.rtt_median) med, AVG(m.rtt_p95) p95, MIN(m.rtt_min) mn, AVG(m.loss_pct) loss
            FROM measurements m JOIN targets t ON t.id = m.target_id
            WHERE t.typ = 'epic' AND m.ts_utc >= ? AND m.ts_utc <= ?
            GROUP BY m.ts_utc ORDER BY m.ts_utc""", (_iso(frm), _iso(to))).fetchall()
        return [dict(r) for r in rows]

    def _bad(self, r: dict) -> bool:
        return (r["med"] is None) or r["med"] > self.cfg.kritisch_ms or (r["loss"] or 0) > self.cfg.verlust_kritisch_pct

    # ------------------------------------------------------------ Endpunkte
    def status(self, q: dict) -> dict:
        cfg = self.cfg
        now = datetime.now(timezone.utc)
        with self.con() as c:
            last = c.execute("SELECT MAX(ts_utc) FROM measurements").fetchone()[0]
            targets = []
            if last:
                for r in c.execute("""SELECT t.name, t.typ, t.resolved, m.* FROM measurements m
                                      JOIN targets t ON t.id = m.target_id WHERE m.ts_utc = ? ORDER BY t.id""", (last,)):
                    targets.append({"name": r["name"], "typ": r["typ"], "host": r["resolved"], "median": r["rtt_median"],
                                    "p95": r["rtt_p95"], "loss": r["loss_pct"], "jitter": r["jitter"]})
            midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            today = self._rounds(c, midnight, now)
            wk = self._rounds(c, now - timedelta(days=7), now)
            prev = self._rounds(c, now - timedelta(days=14), now - timedelta(days=7))
            lastsp = c.execute("SELECT * FROM speedtests WHERE down_mbit > 0 AND up_mbit > 0 ORDER BY ts_utc DESC LIMIT 1").fetchone()
            n_all, first = c.execute("SELECT COUNT(DISTINCT ts_utc), MIN(ts_utc) FROM measurements").fetchone()
        epic = [t for t in targets if t["typ"] == "epic" and t["median"] is not None]
        cur_med = statistics.fmean(t["median"] for t in epic) if epic else None
        cur_loss = statistics.fmean(t["loss"] for t in targets if t["typ"] == "epic") if targets else None
        age_min = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else None
        if last is None:
            ampel = "unbekannt"
        elif cur_med is None or cur_med > cfg.kritisch_ms or (cur_loss or 0) > cfg.verlust_kritisch_pct:
            ampel = "kritisch"
        elif cur_med > cfg.gut_ms:
            ampel = "spuerbar"
        else:
            ampel = "gut"
        share = lambda rs: 100 * sum(self._bad(r) for r in rs) / len(rs) if rs else None  # noqa: E731
        meds_today = [r["med"] for r in today if r["med"] is not None]
        return {
            "jetzt": _iso(now), "letzte_messung": last, "alter_min": age_min,
            "messung_laeuft": age_min is not None and age_min <= 2.5 * cfg.intervall_min,
            "ampel": ampel, "fortnite_median": cur_med, "fortnite_verlust": cur_loss, "ziele": targets,
            "heute_median": statistics.median(meds_today) if meds_today else None,
            "heute_p95": _p(meds_today, 0.95), "heute_runden": len(today),
            "kritisch_7t_pct": share(wk), "kritisch_vorwoche_pct": share(prev),
            "speedtest": dict(lastsp) if lastsp else None,
            "runden_gesamt": n_all, "erste_messung": first,
            "schwellen": {"gut": cfg.gut_ms, "kritisch": cfg.kritisch_ms, "verlust": cfg.verlust_kritisch_pct},
        }

    def series(self, q: dict) -> dict:
        frm, to = self._range(q)
        span_h = (to - frm).total_seconds() / 3600
        bucket = 300 if span_h <= 48 else 1800 if span_h <= 24 * 8 else 7200
        out: dict = {"von": _iso(frm), "bis": _iso(to), "bucket_s": bucket, "serien": []}
        with self.con() as c:
            # Fortnite gesamt (Mittel aller Epic-Ziele je Runde, dann je Zeitfenster)
            rows = c.execute(f"""
                SELECT (CAST(strftime('%s', ts_utc) AS INTEGER) / {bucket}) * {bucket} AS b,
                       AVG(med) med, AVG(p95) p95, MIN(mn) mn, AVG(loss) loss
                FROM (SELECT m.ts_utc, AVG(m.rtt_median) med, AVG(m.rtt_p95) p95, MIN(m.rtt_min) mn, AVG(m.loss_pct) loss
                      FROM measurements m JOIN targets t ON t.id = m.target_id
                      WHERE t.typ='epic' AND m.ts_utc >= ? AND m.ts_utc <= ? GROUP BY m.ts_utc)
                GROUP BY b ORDER BY b""", (_iso(frm), _iso(to))).fetchall()
            out["serien"].append({"name": "Fortnite (Mittel EU-Server)", "typ": "epic-gesamt",
                                  "punkte": [[r["b"], r["med"], r["mn"], r["p95"], r["loss"]] for r in rows]})
            for t in c.execute("SELECT id, name, typ FROM targets WHERE active=1 ORDER BY id").fetchall():
                rows = c.execute(f"""
                    SELECT (CAST(strftime('%s', ts_utc) AS INTEGER) / {bucket}) * {bucket} AS b,
                           AVG(rtt_median) med, MIN(rtt_min) mn, AVG(rtt_p95) p95, AVG(loss_pct) loss
                    FROM measurements WHERE target_id=? AND ts_utc >= ? AND ts_utc <= ?
                    GROUP BY b ORDER BY b""", (t["id"], _iso(frm), _iso(to))).fetchall()
                out["serien"].append({"name": t["name"], "typ": t["typ"],
                                      "punkte": [[r["b"], r["med"], r["mn"], r["p95"], r["loss"]] for r in rows]})
            sp = c.execute("SELECT ts_utc, down_mbit, up_mbit FROM speedtests WHERE down_mbit > 0 AND up_mbit > 0 "
                           "AND ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc", (_iso(frm), _iso(to))).fetchall()
            out["speedtests"] = [[_epoch(r[0]), r[1], r[2]] for r in sp]
            ev = c.execute("SELECT ts_utc, typ, text FROM events WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc",
                           (_iso(frm), _iso(to))).fetchall()
            out["ereignisse"] = [[_epoch(r[0]), r[1], r[2]] for r in ev]
        return out

    def heatmap(self, q: dict) -> dict:
        days = int(q.get("tage", ["28"])[0])
        now = datetime.now(timezone.utc)
        with self.con() as c:
            rounds = self._rounds(c, now - timedelta(days=days), now)
        cells: dict[tuple[int, int], list[float]] = {}
        for r in rounds:
            if r["med"] is None:
                continue
            lt = datetime.fromisoformat(r["ts_utc"]).astimezone()
            cells.setdefault((lt.weekday(), lt.hour), []).append(r["med"])
        grid = [[None] * 24 for _ in range(7)]
        counts = [[0] * 24 for _ in range(7)]
        for (wd, h), v in cells.items():
            grid[wd][h] = statistics.median(v)
            counts[wd][h] = len(v)
        return {"tage": days, "wochentage": WEEKDAYS, "werte": grid, "anzahl": counts}

    def add_note(self, body: dict) -> dict:
        text = str(body.get("text", "")).strip()[:500]
        if not text:
            raise ValueError("Leere Notiz")
        ts = datetime.fromisoformat(body["zeit"]) if body.get("zeit") else datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.astimezone()
        with self.con() as c:
            c.execute("INSERT INTO events(ts_utc, typ, text) VALUES(?,?,?)", (_iso(ts), "Notiz", text))
        return {"ok": True}

    def export_csv(self, q: dict) -> bytes:
        frm, to = self._range(q)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["Zeit (lokal)", "Ziel", "Typ", "Adresse", "Pakete gesendet", "Pakete empfangen", "Verlust %",
                    "Min ms", "Median ms", "Mittel ms", "p95 ms", "Max ms", "Jitter ms"])
        num = lambda v: "" if v is None else f"{v:.1f}".replace(".", ",")  # noqa: E731
        with self.con() as c:
            for r in c.execute("""SELECT m.*, t.name, t.typ, t.resolved FROM measurements m JOIN targets t ON t.id=m.target_id
                                  WHERE m.ts_utc >= ? AND m.ts_utc <= ? ORDER BY m.ts_utc, t.id""", (_iso(frm), _iso(to))):
                lt = datetime.fromisoformat(r["ts_utc"]).astimezone().strftime("%d.%m.%Y %H:%M")
                w.writerow([lt, r["name"], "Fortnite UDP" if r["typ"] == "epic" else "ICMP", r["resolved"], r["sent"],
                            r["received"], num(r["loss_pct"]), num(r["rtt_min"]), num(r["rtt_median"]), num(r["rtt_avg"]),
                            num(r["rtt_p95"]), num(r["rtt_max"]), num(r["jitter"])])
        return ("﻿" + buf.getvalue()).encode("utf-8")


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # nicht ins Konsolenfenster
            log.debug("dashboard: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/static/chart.umd.min.js":
                    self._send(200, (STATIC / "chart.umd.min.js").read_bytes(), "application/javascript")
                elif u.path == "/api/status":
                    self._json(api.status(q))
                elif u.path == "/api/serie":
                    self._json(api.series(q))
                elif u.path == "/api/heatmap":
                    self._json(api.heatmap(q))
                elif u.path == "/api/export.csv":
                    name = f"fortnite-latenz_{datetime.now():%Y-%m-%d}.csv"
                    self._send(200, api.export_csv(q), "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="{name}"'})
                else:
                    self._send(404, b"nicht gefunden", "text/plain")
            except Exception as e:  # noqa: BLE001
                log.exception("Dashboard-Fehler")
                self._json({"fehler": str(e)}, 500)

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                if u.path == "/api/notiz":
                    self._json(api.add_note(body))
                else:
                    self._send(404, b"nicht gefunden", "text/plain")
            except Exception as e:  # noqa: BLE001
                self._json({"fehler": str(e)}, 400)

    return Handler


def start(db_path: Path, cfg: Config, port: int) -> ThreadingHTTPServer | None:
    """Startet den Server im Hintergrund. None, wenn der Port schon belegt ist."""
    try:
        srv = ThreadingHTTPServer((cfg.dashboard_adresse, port), make_handler(Api(db_path, cfg)))
    except OSError as e:
        log.warning("Dashboard-Port %s belegt (%s) - läuft das Programm schon?", port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True).start()
    log.info("Dashboard: http://%s:%s", "localhost" if cfg.dashboard_adresse == "127.0.0.1" else cfg.dashboard_adresse, port)
    return srv
