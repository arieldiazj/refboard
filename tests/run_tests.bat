@echo off
rem Runs the Refboard test suite headless.
rem Usage: tests\run_tests.bat [path-to-blender.exe]
setlocal
set "BLENDER=%~1"
if "%BLENDER%"=="" set "BLENDER=%~dp0..\..\..\..\..\blender.exe"
"%BLENDER%" --background --factory-startup --python "%~dp0test_refboard.py"
exit /b %ERRORLEVEL%
