@echo off
chcp 65001 >nul
cd /d %~dp0
rem 和 tetra 命令同一个启动器：第一次运行会建 .venv、装依赖、问 API Key，然后启动并打开浏览器
python -X utf8 -m mingti.launcher %*
if errorlevel 1 pause
