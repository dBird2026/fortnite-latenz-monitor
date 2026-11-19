@echo off
if not exist "%LOCALAPPDATA%\FortniteLatenzMonitor\config.toml" ( echo Die Datei entsteht beim ersten Start. Bitte zuerst 1_Messung_starten.cmd ausfuehren. & pause & exit /b )
start "" notepad "%LOCALAPPDATA%\FortniteLatenzMonitor\config.toml"
start "" explorer "%LOCALAPPDATA%\FortniteLatenzMonitor"
