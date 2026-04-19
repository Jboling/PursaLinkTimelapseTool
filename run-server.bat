@echo off
title Prusa Snapshot Companion
REM Works from this folder (%~dp0) or from a copy on the Desktop (fixed path below).

if exist "%~dp0run-server.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-server.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Coding\PrusaLinkConnector\run-server.ps1"
)
