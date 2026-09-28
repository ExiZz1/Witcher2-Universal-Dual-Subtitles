# Build Guide — The Witcher 2 Universal Dual Subtitles

## Requirements

- Windows 10/11
- Python 3.13 (64-bit recommended)
- Internet access during the first build so PyInstaller can be installed with pip
- The included Gibbed RED Tools files

## Build

1. Open Command Prompt in this repository folder.
2. Make sure `py` or `python` points to the intended Python installation.
3. Run:

```bat
BUILD_EXE.bat
```

The script:

- removes previous `build` and `dist` folders;
- installs/updates PyInstaller with pip;
- builds a Windows GUI application with PyInstaller in `--onedir` mode;
- copies the `Gibbed RED Tools` directory into the application;
- creates:

```text
dist\Witcher2_Universal_Dual_Subtitles\Witcher2_Universal_Dual_Subtitles.exe
```

## Manual PyInstaller command

The build script uses:

```text
py -m PyInstaller --noconfirm --clean --onedir --windowed --name "Witcher2_Universal_Dual_Subtitles" --add-data "Gibbed RED Tools;Gibbed RED Tools" Witcher2_Dual_Subtitles_Tool.py
```

## Runtime layout

The executable must remain together with the `_internal` directory produced by PyInstaller. The bundled application also needs the `Gibbed RED Tools` directory inside `_internal` as created by the build.

## Notes

The tool is a standalone Windows GUI utility for The Witcher 2: Assassins of Kings. It processes `.w2strings` localization files, merges matching text IDs, and writes the generated localization back to the selected game language slot.

The confirmed subtitle separator used by the final release is:

```text
 ~ 
```

The tool does not modify game font files.
