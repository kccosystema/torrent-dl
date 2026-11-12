# PyInstaller build recipe:  pyinstaller TorrentDL.spec
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("torrentdl/web", "torrentdl/web")]
binaries, hiddenimports = [], ["libtorrent"]
try:  # pywebview: native window (bundled when installed)
    d, b, h = collect_all("webview")
    datas += d; binaries += b; hiddenimports += h
except Exception:
    pass

a = Analysis(["app.py"], datas=datas, binaries=binaries, hiddenimports=hiddenimports,
             excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
icon = "assets/icon.png"

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TorrentDL",
              console=False, icon=icon, argv_emulation=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="TorrentDL")
    app = BUNDLE(coll, name="TorrentDL.app", icon=icon, bundle_identifier="com.kccosystema.torrentdl",
                 info_plist={"CFBundleShortVersionString": "1.0.0", "NSHighResolutionCapable": True,
                             "LSMinimumSystemVersion": "14.0"})
else:  # Windows / Linux: one self-contained file
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="TorrentDL",
              console=False, icon=icon, upx=False)
