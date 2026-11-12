# Building The Witcher 2 Universal Dual Subtitles v3.0

Use official Windows x64 CPython 3.13.15 with Tcl/Tk enabled. The builder needs Python and the pinned dependencies in requirements-build.txt. Players use the portable release.

## Obtain third-party build inputs

- Gibbed RED Tools by Rick. Use the Strings and Unpack EXEs plus Gibbed.IO.dll, Gibbed.RED.FileFormats.dll and NDesk.Options.dll. Gibbed.RED.Pack.exe is only needed for integration fixtures. The [Gibbed tools archive page](https://www.nexusmods.com/witcher2/mods/768?tab=files) is an upstream distribution mirror. Its file revisions may differ. For an exact build, use the copy from the earlier ExiZz distribution or a dependency folder whose files match third-party-lock.json. The staging script accepts an extracted tool directory or a ZIP containing the files. Extract a 7z or installer using its normal tools first.
- Win64 FreeType 2.14.2 from the [Oracle JDK 21.0.12.1 x64 archive](https://download.oracle.com/java/21/archive/jdk-21.0.12.1_windows-x64_bin.zip). Pass the ZIP, the extracted JDK folder or its bin/freetype.dll to the staging script. The application redistributes the FreeType library under its accompanying FTL notice. It uses no Java runtime.
- The OFL Noto outline catalog and supplemental control outline data are committed under resources/. Rebuilding the app uses these source data directly.

The lock file covers third-party build binaries only. It does not set requirements on hashes of game resources. The build accepts exact inputs for reproducibility and the staging script copies only recognized, hash-verified components. It never runs a downloaded installer.

## Build from the repository root

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe prepare_dependencies.py --gibbed 'C:\path\to\Gibbed tools' --freetype 'C:\path\to\JDK.zip'
.\.venv\Scripts\python.exe build_windows.py
```

Use the exact Python version if several are installed. BUILD_EXE.bat is equivalent to invoking build_windows.py. Set W2_BUILD_PYTHON to the desired Python executable if it is not on PATH.

Output is build-v3.0/dist/Witcher2_Universal_Dual_Subtitles/. Keep the EXE next to _internal. The build expands PyInstaller's Python bootstrap modules into _internal so the application directory contains no nested ZIP. Keep the loose .pyc bootstrap modules together with the rest of the runtime. They are generated and absent from source control.

To check reproducibility, run the build twice with the same pinned toolchain and dependency inputs. Use --output to choose separate directories. Compare hashes of each relative path in the resulting application directories. The standalone Nexus distribution adds the player README and the license notices to this output.

See [tests/REPRODUCE.md](tests/REPRODUCE.md) for fixtures and regression commands.
