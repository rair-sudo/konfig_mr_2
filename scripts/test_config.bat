@echo off
cd /d "%~dp0\.."
echo === Vse parametry ===
call run.bat --config config.csv
echo === Oshibka otsutstvuyushchego faila ===
call run.bat --config examples\missing-config.csv
