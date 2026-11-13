@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title 发票自动打印 - 盯 Downloads
echo ============================================================
echo   发票自动打印: 一有新发票 PDF 出现在 Downloads, 就按 A5 打印
echo.
echo   用法: 这个窗口开着别关, 正常去扫码/下载发票即可
echo         扫一张、下载一张, 它就自动打一张
echo.
echo   不会动你的文件 (打完原地保留)
echo   停止: 关掉这个窗口, 或按 Ctrl+C
echo ============================================================
echo.
python tools\print_invoice.py --watch
echo.
echo 打印程序已退出 (按任意键关闭窗口)
pause >nul