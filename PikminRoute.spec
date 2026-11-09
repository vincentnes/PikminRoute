from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata, get_package_paths


_, pytun_path = get_package_paths("pytun_pmd3")
wintun_dir = Path("wintun", "bin", "amd64")

analysis = Analysis(
    ["pikmin_route.py"],
    binaries=[
        (str(Path(pytun_path, wintun_dir, "wintun.dll")), str(Path("pytun_pmd3", wintun_dir))),
    ],
    datas=collect_data_files("pymobiledevice3") + copy_metadata("pymobiledevice3", recursive=True),
    hiddenimports=["pymobiledevice3.cli.mounter"],
    excludes=["IPython"],
)

exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="PikminRoute",
    console=True,
    hide_console="hide-early",
)
