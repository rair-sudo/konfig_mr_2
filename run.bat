@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo Cannot open the project folder. 1>&2
    exit /b 1
)
echo Note: existing SVG and D2 output files may be overwritten.
python -X utf8 -m src.main --force %*
set "result=%errorlevel%"
popd
exit /b %result%
