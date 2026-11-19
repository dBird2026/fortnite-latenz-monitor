"""SQLite-Speicherung."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, typ TEXT NOT NULL,
    host TEXT NOT NULL, resolved TEXT, port INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id),
    ts_utc TEXT NOT NULL, sent INTEGER, received INTEGER, loss_pct REAL,
    rtt_min REAL, rtt_median REAL, rtt_avg REAL, rtt_p95 REAL, rtt_max REAL, jitter REAL, error TEXT);
CREATE INDEX IF NOT EXISTS ix_meas_ts ON measurements(ts_utc);
CREATE INDEX IF NOT EXISTS ix_meas_target_ts ON measurements(target_id, ts_utc);
CREATE TABLE IF NOT EXISTS samples (
    measurement_id INTEGER NOT NULL REFERENCES measurements(id), seq INTEGER, rtt_ms REAL);
CREATE TABLE IF NOT EXISTS speedtests (
    id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, down_mbit REAL, up_mbit REAL, ping_ms REAL, server TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, typ TEXT NOT NULL, text TEXT);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        self.con.commit()

    def upsert_target(self, name: str, typ: str, host: str, resolved: str | None, port: int) -> int:
        self.con.execute(
            "INSERT INTO targets(name, typ, host, resolved, port, active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(name) DO UPDATE SET typ=excluded.typ, host=excluded.host, resolved=excluded.resolved, "
            "port=excluded.port, active=1", (name, typ, host, resolved, port))
        self.con.commit()
        return self.con.execute("SELECT id FROM targets WHERE name=?", (name,)).fetchone()[0]

    def deactivate_others(self, names: list[str]) -> None:
        q = ",".join("?" * len(names)) or "''"
        self.con.execute(f"UPDATE targets SET active=0 WHERE name NOT IN ({q})", names)
        self.con.commit()

    def add_measurement(self, target_id: int, ts: str, stats: dict, rtts: list, error: str = "") -> None:
        cur = self.con.execute(
            "INSERT INTO measurements(target_id, ts_utc, sent, received, loss_pct, rtt_min, rtt_median, rtt_avg, "
            "rtt_p95, rtt_max, jitter, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (target_id, ts, stats["sent"], stats["received"], stats["loss_pct"], stats["rtt_min"], stats["rtt_median"],
             stats["rtt_avg"], stats["rtt_p95"], stats["rtt_max"], stats["jitter"], error or None))
        self.con.executemany("INSERT INTO samples(measurement_id, seq, rtt_ms) VALUES(?,?,?)",
                             [(cur.lastrowid, i, r) for i, r in enumerate(rtts)])
        self.con.commit()

    def add_speedtest(self, ts: str, res: dict | None, error: str = "") -> None:
        res = res or {}
        self.con.execute("INSERT INTO speedtests(ts_utc, down_mbit, up_mbit, ping_ms, server, error) VALUES(?,?,?,?,?,?)",
                         (ts, res.get("down_mbit"), res.get("up_mbit"), res.get("ping_ms"), res.get("server"), error or None))
        self.con.commit()

    def add_event(self, typ: str, text: str, ts: str | None = None) -> None:
        self.con.execute("INSERT INTO events(ts_utc, typ, text) VALUES(?,?,?)", (ts or now_utc(), typ, text))
        self.con.commit()

    def last_ts(self) -> str | None:
        row = self.con.execute("SELECT MAX(ts_utc) FROM measurements").fetchone()
        return row[0] if row else None

    def last_speedtest_ts(self) -> str | None:
        row = self.con.execute("SELECT MAX(ts_utc) FROM speedtests").fetchone()
        return row[0] if row else None

    def recent_loss(self, target_id: int, n: int) -> list[float]:
        rows = self.con.execute("SELECT loss_pct FROM measurements WHERE target_id=? ORDER BY ts_utc DESC LIMIT ?",
                                (target_id, n)).fetchall()
        return [r[0] for r in rows]

    def housekeeping(self, sample_days: int = 90) -> None:
        self.con.execute("DELETE FROM samples WHERE measurement_id IN (SELECT id FROM measurements "
                         "WHERE ts_utc < datetime('now', ?))", (f"-{sample_days} days",))
        self.con.commit()

    def daily_backup(self) -> None:
        bak = self.path.with_name("data-backup.db")
        if not bak.exists() or datetime.now().timestamp() - bak.stat().st_mtime > 86400:
            tmp = sqlite3.connect(bak)
            with tmp:
                self.con.backup(tmp)
            tmp.close()
