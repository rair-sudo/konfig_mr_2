@echo off
cd /d "%~dp0\.."
echo === Acyclic graph ===
call run.bat --config examples\config_acyclic.csv --reverse LAMBDA
if errorlevel 1 exit /b 1
echo === Graph with cycles ===
call run.bat --config examples\config_cycle.csv --reverse EPSILON
if errorlevel 1 exit /b 1
echo === Filtered graph ===
call run.bat --config examples\config_filter.csv --reverse SHARED
if errorlevel 1 exit /b 1
exit /b 0
