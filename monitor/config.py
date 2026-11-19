"""Konfiguration laden (config.toml im Datenordner, wird beim ersten Start angelegt)."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "FortniteLatenzMonitor"

DEFAULT_CONFIG = """\
# Fortnite-Latenz-Monitor - Konfiguration
# Nach Änderungen das Programm neu starten.

[messung]
intervall_minuten = 5        # alle 5 Minuten eine Messrunde
pakete_pro_messung = 20      # Pakete je Ziel und Runde
paket_abstand_ms = 50        # Abstand zwischen den Paketen
timeout_ms = 1000            # Wartezeit auf die letzte Antwort

[speedtest]
aktiv = true
intervall_minuten = 60

[system]
wach_halten = true           # Standby verhindern, solange das Programm läuft

[dashboard]
port = 8765                  # http://localhost:8765
adresse = "127.0.0.1"        # "0.0.0.0" = auch von anderen Geräten im Heimnetz erreichbar
browser_oeffnen = true       # beim Start automatisch im Browser öffnen

[schwellen]                  # Bewertung für Fortnite
gut_ms = 40
kritisch_ms = 80
verlust_kritisch_pct = 1.0

# ---------------------------------------------------------------- Messziele
# typ = "icmp" (normaler Ping) oder "epic" (Fortnite-QoS-Paket an UDP 22222)
# host = "auto-router" / "auto-provider" wird beim Start per traceroute ermittelt.
# Die Epic-IPs stammen aus dem Mitschnitt vom 08.11.2026.

[[ziel]]
name = "Router"
typ = "icmp"
host = "auto-router"

[[ziel]]
name = "Provider-Knoten"
typ = "icmp"
host = "auto-provider"

[[ziel]]
name = "Referenz 1.1.1.1"
typ = "icmp"
host = "1.1.1.1"

[[ziel]]
name = "Fortnite Frankfurt A"
typ = "epic"
host = "3.66.90.173"

[[ziel]]
name = "Fortnite Frankfurt B"
typ = "epic"
host = "3.66.90.162"

[[ziel]]
name = "Fortnite Frankfurt C"
typ = "epic"
host = "3.66.90.156"

[[ziel]]
name = "Fortnite London"
typ = "epic"
host = "18.133.162.149"

[[ziel]]
name = "Fortnite Paris"
typ = "epic"
host = "15.237.20.100"

[[ziel]]
name = "Fortnite Belgien"
typ = "epic"
host = "34.79.207.54"
"""


@dataclass
class Target:
    name: str
    typ: str  # "icmp" | "epic"
    host: str
    port: int = 22222


@dataclass
class Config:
    intervall_min: int = 5
    pakete: int = 20
    abstand_ms: int = 50
    timeout_ms: int = 1000
    speedtest_aktiv: bool = True
    speedtest_intervall_min: int = 60
    wach_halten: bool = True
    gut_ms: float = 40
    kritisch_ms: float = 80
    verlust_kritisch_pct: float = 1.0
    dashboard_port: int = 8765
    dashboard_adresse: str = "127.0.0.1"
    browser_oeffnen: bool = True
    ziele: list[Target] = field(default_factory=list)


def data_dir() -> Path:
    """Datenordner: FLM_DATA_DIR (Container: /data), sonst %LOCALAPPDATA% bzw. ~/.local/share."""
    if os.environ.get("FLM_DATA_DIR"):
        d = Path(os.environ["FLM_DATA_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        return d
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path | None = None) -> tuple[Config, Path]:
    path = path or data_dir() / "config.toml"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    m, s, sy, sw, da = (raw.get(k, {}) for k in ("messung", "speedtest", "system", "schwellen", "dashboard"))
    cfg = Config(
        intervall_min=int(m.get("intervall_minuten", 5)),
        pakete=int(m.get("pakete_pro_messung", 20)),
        abstand_ms=int(m.get("paket_abstand_ms", 50)),
        timeout_ms=int(m.get("timeout_ms", 1000)),
        speedtest_aktiv=bool(s.get("aktiv", True)),
        speedtest_intervall_min=int(s.get("intervall_minuten", 60)),
        wach_halten=bool(sy.get("wach_halten", True)),
        gut_ms=float(sw.get("gut_ms", 40)),
        kritisch_ms=float(sw.get("kritisch_ms", 80)),
        verlust_kritisch_pct=float(sw.get("verlust_kritisch_pct", 1.0)),
        dashboard_port=int(da.get("port", 8765)),
        dashboard_adresse=os.environ.get("FLM_BIND") or str(da.get("adresse", "127.0.0.1")),
        browser_oeffnen=bool(da.get("browser_oeffnen", True)) and os.environ.get("FLM_BROWSER", "1") != "0",
        ziele=[Target(z["name"], z["typ"], z["host"], int(z.get("port", 22222))) for z in raw.get("ziel", [])],
    )
    return cfg, path
