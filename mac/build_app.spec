# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CresnetMon macOS .app bundle.

Build:  uv run --group packaging pyinstaller build_app.spec
Output: dist/CresnetMon.app

No custom icon yet (PyInstaller/macOS default is used) - deferred, tracked
in STRATEGY.md; not a blocker for a working, launchable bundle.
"""

a = Analysis(
    ["src/cresnetmon/main.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CresnetMon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CresnetMon",
)

app = BUNDLE(
    coll,
    name="CresnetMon.app",
    icon=None,
    bundle_identifier="com.pdehlke.cresnetmon",
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "0.1.0",
        "NSHumanReadableCopyright": "",
    },
)
