@echo off
cd /d "%~dp0"
python run.py gui
if errorlevel 1 pause
