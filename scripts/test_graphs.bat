@echo off
cd /d "%~dp0\.."
for %%C in (acyclic cycle filter) do (
    echo === %%C ===
    call run.bat --config examples\config_%%C.csv --force
    if errorlevel 1 exit /b 1
)
exit /b 0
