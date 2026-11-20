@echo off
cd /d "%~dp0\.."
python -m unittest tests.test_config -v
exit /b %errorlevel%
