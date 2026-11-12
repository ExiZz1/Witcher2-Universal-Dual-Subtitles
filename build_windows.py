"""Build the Windows x64 portable distribution with pinned tools."""
import argparse
import importlib.metadata as metadata
import os
from pathlib import Path
import struct
import subprocess
import sys
from zipfile import ZipFile

from prepare_dependencies import validate_dependencies


ROOT = Path(__file__).resolve().parent
APP = "Witcher2_Universal_Dual_Subtitles"


def normalize_runtime(dist):
    internal = dist / "_internal"
    base_library = internal / "base_library.zip"
    with ZipFile(base_library) as archive:
        for member in archive.namelist():
            target = internal / member
            if not target.resolve().is_relative_to(internal.resolve()) or target.exists():
                raise RuntimeError(f"Invalid or duplicate standard library member: {member}")
            if member.endswith("/"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    base_library.unlink()
    # Both files define the same table. Windows uses cp936 for its system
    # encoding and the application does not request the alternate Tcl name.
    encodings = internal / "_tcl_data/encoding"
    alias = encodings / "euc-cn.enc"
    if alias.read_bytes() == (encodings / "gb2312.enc").read_bytes():
        alias.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build-v3.0")
    args = parser.parse_args()
    out = args.output.resolve()
    if sys.platform != "win32" or sys.version_info[:3] != (3, 13, 15) or struct.calcsize("P") != 8:
        parser.error("Build with Python 3.13.15 Windows x64.")
    for row in (ROOT / "requirements-build.txt").read_text("utf-8").splitlines():
        if row and not row.startswith("#"):
            name, expected = row.split("==")
            if metadata.version(name) != expected:
                parser.error(f"Expected {name}=={expected}.")
    try:
        validate_dependencies(ROOT)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    env = dict(os.environ, PYTHONHASHSEED="0", SOURCE_DATE_EPOCH="1794441600")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onedir", "--windowed", "--noupx", "--exclude-module", "ssl", "--name", APP,
               "--manifest", str(ROOT / "windows.manifest"), "--version-file", str(ROOT / "windows-version.txt"),
               "--distpath", str(out / "dist"), "--workpath", str(out / "work"), "--specpath", str(out)]
    for file in ("control-donor-codes.json", "donor.json", "freetype.dll"):
        command.extend(("--add-data", f"{ROOT / 'resources' / file};resources"))
    for file in ("Gibbed.RED.Strings.exe", "Gibbed.RED.Unpack.exe", "Gibbed.RED.FileFormats.dll",
                 "Gibbed.IO.dll", "NDesk.Options.dll"):
        command.extend(("--add-data", f"{ROOT / 'Gibbed RED Tools' / file};Gibbed RED Tools"))
    command.append(str(ROOT / "Witcher2_Dual_Subtitles_Tool.py"))
    code = subprocess.run(command, cwd=ROOT, env=env).returncode
    if code == 0:
        normalize_runtime(out / "dist" / APP)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
