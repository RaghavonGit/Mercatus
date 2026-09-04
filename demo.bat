@echo off
REM One-command Mercatus autopay demo. See Mercator\AUTOPAY.md.
REM   demo.bat            set up + launch + open the browser
REM   demo.bat --verify   headless: run the flow, print a PASS/FAIL checklist
where uv >nul 2>nul || (echo uv is not installed - see https://docs.astral.sh/uv/ & exit /b 1)
pushd "%~dp0Forum"
uv run demo %*
popd
