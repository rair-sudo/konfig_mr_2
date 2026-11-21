@echo off
cd /d "%~dp0\.."
where mvn >nul 2>&1
if errorlevel 1 (
    echo Install Apache Maven and add mvn.cmd to PATH for comparison only.
    exit /b 1
)
for %%P in (junit commons-text slf4j-api) do (
    echo === Maven dependency:tree: %%P ===
    call mvn -B -ntp -f examples\comparison\%%P\pom.xml ^
      org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree ^
      -Dscope=runtime -DoutputType=text
    if errorlevel 1 exit /b 1
)
exit /b 0
