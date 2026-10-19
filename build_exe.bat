@echo off
echo ==================================================
echo Starting PyInstaller packaging for xtell-serial...
echo   -> xtell-serial.exe      (GUI, windowed, with icons)
echo   -> xtell-serial-mcp.exe  (MCP server, console)
echo ==================================================

:: Clean previous build artifacts
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

python -m PyInstaller --clean --noconfirm xtell_serial.spec
if %errorlevel% neq 0 goto :failed

python -m PyInstaller --clean --noconfirm xtell_serial_mcp.spec
if %errorlevel% neq 0 goto :failed

if not exist dist\xtell-serial.exe goto :failed
if not exist dist\xtell-serial-mcp.exe goto :failed

echo.
echo ==================================================
echo Packaging SUCCESSFUL! Output is at:
echo   dist\xtell-serial.exe
echo   dist\xtell-serial-mcp.exe
echo.
echo Register xtell-serial-mcp.exe (NOT the GUI) in mcpServers.
echo ==================================================
goto :end

:failed
echo.
echo ==================================================
echo Packaging FAILED! Please check error output.
echo ==================================================

:end
pause
