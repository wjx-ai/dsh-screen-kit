@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title 发票自助打印
python tools\scan_console.py
if errorlevel 1 pause