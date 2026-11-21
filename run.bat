@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m src.main %*
exit /b %errorlevel%
