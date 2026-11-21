@echo off
cd /d "%~dp0\.."
for %%C in (junit commons_text slf4j_api) do (
    echo === %%C ===
    call run.bat --config examples\%%C.csv --force
    if errorlevel 1 exit /b 1
)
exit /b 0
