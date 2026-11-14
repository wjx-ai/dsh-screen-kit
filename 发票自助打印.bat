@echo off
rem 必须用 conhost 启动: Windows 11 默认由 Windows Terminal 托管控制台,
rem 在那边 SetCurrentConsoleFontEx 会被静默忽略, 字号永远是 16。
rem 走 conhost 才能用上 32 号字。
cd /d "%~dp0"
start "" conhost.exe cmd.exe /c "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & python tools\scan_console.py & pause"
exit