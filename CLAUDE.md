# CLAUDE.md – Fortnite-Latenz-Monitor

## Projekt in einem Satz
Dirk (Repo-Owner `dBird2026`) ist privat bei **Cable4** (Kabelnetz) und hat abends hohe Latenzen in Fortnite.
Dieses Tool misst das dauerhaft, damit er sich mit Daten und Grafiken bei Cable4 beschweren kann.

## Zusammenarbeit
- **Immer auf Deutsch** antworten, knapp und konkret. UI-Texte, Logs, Kommentare: Deutsch.
- Dirk ist technisch versiert, aber kein Python-Entwickler: Anleitungen Schritt für Schritt.
- **Deployment = Push auf `main`.** GitHub Actions baut Image + Windows-ZIP; die NAS zieht das Image per
  Watchtower selbst (stündlich). Vor jedem Push: `python -m pyflakes monitor tests && python -m pytest -q`.
- Nach dem Push den Actions-Lauf prüfen (`gh run list`, `gh run view --log-failed`) und Fehler beheben.
- Niemals Zugangsdaten, Tokens oder Messdaten (`*.db`) committen.

## Architektur
- `monitor/` – Python 3.12, nur Standardbibliothek + `speedtest-cli` (+ `icmplib` unter Linux)
  - `probes.py` – Epic-QoS-UDP (22 Byte, Port 22222, Server spiegeln das Paket), ICMP
    (Windows: `IcmpSendEcho` per ctypes ohne Admin; Linux: icmplib), traceroute-Auswertung, Speedtest
  - `service.py` – Messrunde alle 5 Min am Uhr-Raster, Speedtest stündlich nach der Runde, Lücken-Events,
    Warnung wenn ein Epic-Ziel 1 h nicht antwortet, Standby-Sperre (Windows)
  - `db.py` – SQLite (WAL): `targets`, `measurements`, `samples` (90 Tage), `speedtests`, `events`
  - `dashboard.py` + `static/index.html` – `ThreadingHTTPServer`, JSON-API, Chart.js 4.4.6 lokal gebündelt
  - `config.py` – `config.toml` im Datenordner (wird beim ersten Start angelegt)
- Datenordner: `FLM_DATA_DIR` (Container `/data`), sonst `%LOCALAPPDATA%\FortniteLatenzMonitor`
- Umgebungsvariablen: `FLM_BIND` (Dashboard-Adresse, Container `0.0.0.0`), `FLM_BROWSER=0` (kein Browser)
- Auslieferung:
  - **NAS**: QNAP TS-451A (x86_64), Container Station, `nas/docker-compose.yml`, `network_mode: host`,
    Daten in `/share/Container/fortnite-latenz-monitor/data`, Watchtower-Fork `nickfedor/watchtower`
    (das Original containrrr ist seit Dez. 2025 eingestellt). Dashboard `http://<NAS-IP>:8765`.
  - **Windows**: PyInstaller onedir (`run_monitor.py`), ZIP mit `.cmd`-Startern aus `windows/`;
    für andere Nutzer gedacht, muss weiter funktionieren.

## Messergebnisse / Fakten aus Phase 0 (08.11.2026)
- Fortnite pingt ~90 Server weltweit an UDP 22222; DNS-Namen `qosping-*` existieren nicht mehr.
- EU-Ziele: Frankfurt 3.66.90.173/.162/.156, London 18.133.162.149, Paris 15.237.20.100, Belgien 34.79.207.54.
- Dirks Heimnetz: Router 192.168.168.1, erster Cable4-Knoten 100.127.224.2 (CGNAT); schon dort abends
  Median ~47 ms, Spitzen >100 ms, Router 0–1 ms.

## Konzept & Stand
Konzept-Dokument: https://claude.ai/artifact/9r7Eou9naPuRANAb3wTPdh
- [x] Phase 0 Verifikation, Phase 1 Messdienst, Phase 2 Dashboard
- [x] 0.3: Container-Version, GitHub Actions (Image + Windows-exe), NAS-Compose mit Watchtower
- [ ] **Phase 3 – Beschwerde-Report:** PDF (2–3 Seiten) für frei wählbaren Zeitraum: Kopf mit Kundendaten
      aus config, Kennzahlen tags/abends, Grafiken Verlauf + Tagesgang-Heatmap + Abgrenzung
      Router/Cable4/Fortnite, 10 schlechteste Zeitfenster, Messaufbau, Lücken/Notizen; plus Textbaustein.
      Im Dashboard per Knopf. Muss im Container und in der exe laufen (z. B. matplotlib + reportlab).
- [ ] Phase 4 – Windows-Komfort: Tray-Symbol, Autostart (Aufgabenplanung). Auf der NAS nicht nötig.
- Bekannt: Speedtest liefert direkt nach dem Hochfahren manchmal 0 → wird als Fehler gespeichert und
  im Dashboard ausgeblendet.
