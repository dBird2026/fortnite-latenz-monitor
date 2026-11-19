@echo off
chcp 65001 >nul
rem Laeuft die Messung schon, wird nur der Browser geoeffnet. Sonst startet ein Dashboard ohne Messung.
"%~dp0FortniteLatenzMonitor.exe" dashboard
