"""Headless packaged P3 Install and Restore checks on temporary fixtures."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from w2_font_test import archive_stamp, profile_files
from w2_install import digest
from smoke_p2_frozen import snapshot

CODES = ("EN-ZH", "ZH-EN", "PL-ZH", "ZH-PL", "JP-ZH", "ZH-JP", "TR-JP", "JP-TR", "TR-ZH", "ZH-TR", "RU-JP", "JP-RU")


def clean_environment():
    excluded = ("PYTHON", "PYINSTALLER", "_PYI", "TCL_", "TK_", "DOTNET", "VIRTUAL_ENV",
                "CONDA", "COMPLUS_")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(excluded)}
    system = Path(os.environ["SystemRoot"])
    env["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    return env


def launch(exe, game, folder, action, pair, *flags):
    report = folder / "result.json"
    rc = subprocess.run([str(exe), "--cli", action, "--game", str(game), "--pair", pair,
                         "--report", str(report), *flags], timeout=150, cwd=folder, env=clean_environment()).returncode
    return rc, json.loads(report.read_text("utf-8"))


def build_fixture(root, strings, assets):
    game = root / "Test Game – Żółć 日本語"
    cooked = game / "CookedPC"
    gui = cooked / "globals/gui"
    gui.mkdir(parents=True)
    selected = root / "stock"
    names = ("fonts.swf", "fonts_jp.swf", "fonts_zh.swf", "cfonts.swf", "fonts/fonts.csv",
             "ui_mainmenu.swf", "ui_dialog.swf", "ui_journal.swf", "hud.swf")
    for name in names:
        original = assets / "globals/gui" / name
        if not original.exists() and name == "cfonts.swf":
            continue
        member = selected / "globals/gui" / name
        member.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, member)
        if name.startswith("fonts") and name.endswith(".swf") and name != "fonts_zh.swf":
            shutil.copyfile(original, gui / name)
    for slot in ("en0", "pl0", "jp0", "zh0", "tr0", "ru0"):
        shutil.copyfile(strings / (slot + ".w2strings"), cooked / (slot + ".w2strings"))
    pack_fixture(selected, cooked / "pack0.dzip")
    return game, cooked, selected


def pack_fixture(selected, archive):
    archive.unlink(missing_ok=True)
    subprocess.run([str(ROOT / "Gibbed RED Tools/Gibbed.RED.Pack.exe"), str(archive), str(selected)],
                   timeout=50, capture_output=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("strings", type=Path)
    parser.add_argument("assets", type=Path)
    parser.add_argument("--live", type=Path)
    parser.add_argument("--runtime-report", type=Path)
    parser.add_argument("--previous-exe", type=Path, help="Verify Restore of an active earlier installation")
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    with tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-smoke-") as name:
        folder = Path(name)
        staged = folder / "Extracted – Польська 日本語"
        shutil.copytree(exe.parent, staged)
        exe = staged / exe.name
        runtime = folder / "runtime.json"
        env = clean_environment()
        assert not any(shutil.which(cmd, path=env["PATH"]) for cmd in ("python", "pip", "dotnet", "cl", "msbuild"))
        rc = subprocess.run([str(exe), "--runtime-check", str(runtime)], cwd=folder, env=env, timeout=40)
        data = json.loads(runtime.read_text("utf-8"))
        assert rc.returncode == 0 and data["status"] == "ok", data
        if args.runtime_report:
            args.runtime_report.write_bytes(runtime.read_bytes())
        print("PASS bundled Python, Tcl/Tk and FreeType loaded from relocated package with system-only PATH", flush=True)
        game, cooked, selected = build_fixture(folder, args.strings, args.assets)
        clean = snapshot(cooked)
        stamps = archive_stamp(cooked)
        if args.previous_exe:
            previous_exe = args.previous_exe.resolve(strict=True)
            rc, report = launch(previous_exe, game, folder, "apply", "ZH-JP", "--accept-clean-sources")
            assert rc == 0 and report["applied"], report
            active = snapshot(cooked, include_state=True)
            rc, report = launch(exe, game, folder, "apply", "EN-ZH", "--accept-clean-sources")
            assert rc == 1 and "active" in report["error"] and snapshot(cooked, include_state=True) == active
            rc, report = launch(exe, game, folder, "restore", "EN-ZH")
            assert rc == 0 and report["restored"] and snapshot(cooked) == clean
            assert archive_stamp(cooked) == stamps
            print("PASS previous EXE journal detected and restored byte for byte", flush=True)
            # Run the ordinary smoke against fresh provenance.
            shutil.rmtree(game)
            game, cooked, selected = build_fixture(folder, args.strings, args.assets)
            clean = snapshot(cooked)
            stamps = archive_stamp(cooked)
        rc, report = launch(exe, game, folder, "apply", "EN-ZH")
        assert rc == 1 and "No verified" in report["error"] and snapshot(cooked) == clean
        print("PASS first-time source confirmation required before writing", flush=True)
        for pair in CODES:
            p, s = pair.split("-")
            rc, report = launch(exe, game, folder, "apply", pair, "--accept-clean-sources")
            assert rc == 0 and report["applied"], (pair, report)
            assert report["primary_slot"] == profile_files(p)[0] and report["primary_text_language"] == p
            assert report["secondary"] == s and report["changed_ids"] and report["required_codes"]
            if s == "ZH":
                assert all(face["zh_adjusted_stock_codes"] for face in report["faces"]), (pair, report)
            else:
                assert not any(face["zh_adjusted_stock_codes"] for face in report["faces"])
            if p == "ZH":
                assert all(face["adjusted_existing"] for face in report["faces"]), (pair, report)
            else:
                assert not any(face["adjusted_existing"] for face in report["faces"])
            changed = set(snapshot(cooked).items()) - set(clean.items())
            assert {key for key, _ in changed} <= set(profile_files(p))
            assert profile_files(p)[0] in {key for key, _ in changed}
            applied = snapshot(cooked, include_state=True)
            rc, refused = launch(exe, game, folder, "apply", s + "-" + p, "--accept-clean-sources")
            assert rc == 1 and "active" in refused["error"] and snapshot(cooked, include_state=True) == applied
            rc, restored = launch(exe, game, folder, "restore", s + "-" + p)
            assert rc == 0 and restored["restored"] and snapshot(cooked) == clean
            assert archive_stamp(cooked) == stamps
            print(f"PASS packaged {pair} Primary-only Install, cross-pair refusal, byte-exact Restore", flush=True)

        missing = cooked / "zh0.w2strings"
        missing.unlink()
        expected = snapshot(cooked)
        rc, report = launch(exe, game, folder, "apply", "EN-ZH", "--accept-clean-sources")
        assert rc == 1 and "Missing localization: zh0.w2strings" in report["error"] and snapshot(cooked) == expected
        print("PASS missing localization refused", flush=True)
        shutil.copyfile(args.strings / missing.name, missing)
        (selected / "globals/gui/fonts_zh.swf").unlink()
        pack_fixture(selected, cooked / "pack0.dzip")
        rc, report = launch(exe, game, folder, "apply", "EN-ZH", "--accept-clean-sources")
        assert rc == 1 and "fonts_zh.swf" in report["error"] and snapshot(cooked) == clean
        print("PASS missing donor library refused", flush=True)

        rc, report = launch(exe, game, folder, "apply", "XX-EN", "--accept-clean-sources")
        assert rc == 1 and "Unsupported language profile combination" in report["error"]
        assert snapshot(cooked) == clean
        print("PASS unsupported pair refused", flush=True)

        if args.live:
            cooked = args.live / "CookedPC"
            clean = snapshot(cooked, include_state=True, live=True)
            stamps = archive_stamp(cooked)
            for pair in ("EN-ZH", "ZH-EN", "JP-ZH", "ZH-JP", "TR-JP", "JP-TR", "PL-JP", "JP-PL"):
                rc, report = launch(exe, args.live, folder, "prepare", pair, "--accept-clean-sources")
                assert rc == 0 and not report["applied"], (pair, report)
                print(f"PASS installed game read-only Prepare {pair}", flush=True)
            assert snapshot(cooked, include_state=True, live=True) == clean and archive_stamp(cooked) == stamps
            print(f"Installed game unchanged ({len(clean)} digests, {len(stamps)} archive stamps).", flush=True)


if __name__ == "__main__":
    main()
