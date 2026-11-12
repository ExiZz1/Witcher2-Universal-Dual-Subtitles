# Reproducing the v3.0 checks

Use the pinned Windows environment in [BUILD_GUIDE.md](../BUILD_GUIDE.md).
Stage Gibbed with `prepare_dependencies.py --test-tools` for fixture packing.
Supply only copies of game files. Write all outputs outside the game folder.

## Inputs

- `W2_TEST_COPY_SOURCE` points to the 12 clean .w2strings slots.
- `W2_P3_ASSETS` points to extracted pack0.dzip assets, with globals/gui under that directory.
- `W2_P2_FIXTURE` contains assets and copy/CookedPC. Use Gibbed.RED.Unpack.exe -e EXT PACK0 DEST to extract SWF, CSV and w2scene resources into assets, preserving relative paths. Include globals/gui/fonts.swf, fonts_jp.swf, fonts_zh.swf, cfonts.swf, ui_mainmenu.swf, ui_dialog.swf, ui_journal.swf, hud.swf and globals/gui/fonts/fonts.csv, and q000_wakeup_cs.w2scene and q000_camp_choice.w2scene in their original directories. Pack assets into copy/CookedPC/pack0.dzip using Gibbed.RED.Pack.exe OUTPUT_DZIP ASSETS. Copy assets/globals into copy/CookedPC/globals and copy the clean pl0.w2strings and jp0.w2strings into copy/CookedPC.
- `W2_MATRIX_RESULTS` points to the output directory of run_font_matrix.py. The Chinese outline tests use its EN-ZH donor cohort.
- `W2_ZH_REPORTS` points to an existing directory for Chinese geometry results.
- `W2_P0_SOURCE` optionally points to the historical P0 TEST 1 source for the cross-version journal fixture. Its one compatibility case is skipped without it.

## Commands

Run from the repository root. Use an empty output directory for each matrix.

```powershell
python tests/run_font_matrix.py GAME_STRINGS PACK0_ASSETS RESULTS --workers 6
python tests/test_p0.py
python tests/test_p1.py
python tests/test_p2.py
python tests/test_p2_corpus.py
python tests/test_p3.py
python tests/test_zh_ink.py
python tests/test_release.py
python tests/smoke_p3.py FROZEN_EXE GAME_STRINGS PACK0_ASSETS --runtime-report RUNTIME_JSON
python tests/audit_runtime.py DIST_DIRECTORY REBUILD_DIRECTORY GAME_STRINGS/en0.w2strings DEPENDENCY_JSON
```

To verify upgrade and byte-exact Restore of an active installation, pass `--previous-exe` to smoke_p3.py. Use an earlier distribution's EXE alongside its dependencies. The smoke stages a Unicode path and runs the application with only Windows directories on PATH. Its driver runs in the builder's Python. The application loads Python, Tcl/Tk, its bootstrap modules and FreeType from its distribution.

The window tests exercise DPI geometry and Win32 resize policy without opening or automating the application GUI. The matrix writes and restores temporary copies in all ordered directions. Compare primary_slot, localization_sha256 and font_sha256 across builds to check output equivalence.
