# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['xtell_serial_mcp.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['mcp', 'mcp.server.stdio', 'anyio', 'serial.tools.list_ports'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL', 'pystray'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='xtell-serial-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # MCP 走 stdio, console 必须为 True。改成 False 会让 sys.stdin 变成 None
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
