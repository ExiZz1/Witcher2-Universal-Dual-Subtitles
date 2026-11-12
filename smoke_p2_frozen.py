"""Verify the frozen --p2 entry point without launching the app window or game."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from w2_font_test import FontTransaction, archive_stamp, profile_files
from w2_install import GameState, LEGACY_DIR, STATE_DIR, digest


def snapshot(cooked, include_state=False, live=False):
    if live:
        paths = list(cooked.glob("*.w2strings"))
        for relative in ("globals/gui", LEGACY_DIR, STATE_DIR):
            paths.extend((cooked / relative).rglob("*"))
    else:
        paths = cooked.rglob("*")
    return {p.relative_to(cooked).as_posix(): digest(p.read_bytes()) for p in paths
            if p.is_file() and p.suffix.lower() != ".dzip"
            and (include_state or "_ExiZz_DualSubtitles_State" not in p.parts)}


def invoke(exe, action, pair, game, report):
    return subprocess.run([str(exe), "--p2", action, "--game", str(game), "--pair", pair,
                           "--report", str(report)], timeout=120).returncode


def report_at(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--live-active", type=Path, help="Optional selected installation with an active P2 journal")
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)

    with tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-p2-frozen-") as temp:
        root = Path(temp)
        game = root / "steamapps/common/Test Installation"
        shutil.copytree(args.fixture / "copy", game, ignore=shutil.ignore_patterns("_ExiZz_DualSubtitles_State"))
        state = GameState(game)
        with state.locked():
            slots = {name: {"original": state.remember((state.cooked / name).read_bytes()), "result": None}
                     for name in (profile_files(lang)[0] for lang in ("PL", "JP"))}
            state.save({**state.state, "slots": slots})
        baseline = snapshot(state.cooked)
        stamps = archive_stamp(state.cooked)
        for pair in ("PL-JP", "JP-PL"):
            selected, secondary = pair.split("-")
            expected = set(profile_files(selected))
            result = root / (pair + ".json")
            assert invoke(exe, "prepare", pair, game, result) == 0
            prepared = report_at(result)
            assert not prepared["applied"] and prepared["p1_eligible"] == 59908
            assert len(prepared["changed_ids"]) == 59906 and len(prepared["skipped_glyph_records"]) == 2
            assert prepared["primary_text_language"] == selected and prepared["secondary"] == secondary
            assert snapshot(state.cooked) == baseline
            print(f"PASS frozen fixture Prepare {pair}", flush=True)

            unavailable = root / "missing-directory/result.json"
            assert invoke(exe, "apply", pair, game, unavailable) == 1
            assert snapshot(state.cooked) == baseline
            assert not FontTransaction(state).journal.exists()
            print(f"PASS frozen fixture refuses Apply without a writable report {pair}", flush=True)

            assert invoke(exe, "apply", pair, game, result) == 0
            applied = report_at(result)
            assert applied["applied"] and applied["changed_ids"] == prepared["changed_ids"]
            assert {name for name, value in snapshot(state.cooked).items() if baseline.get(name) != value} == expected
            print(f"PASS frozen fixture Apply {pair}", flush=True)

            patched = snapshot(state.cooked, include_state=True)
            assert invoke(exe, "apply", pair, game, result) == 1
            assert "P2/P3 install or recovery is active" in report_at(result)["error"]
            assert snapshot(state.cooked, include_state=True) == patched
            print(f"PASS frozen fixture active P2 rejects reapply {pair}", flush=True)

            assert invoke(exe, "restore", "-".join(reversed(pair.split("-"))), game, result) == 0
            assert report_at(result) == {"restored": True}
            assert snapshot(state.cooked) == baseline
            assert archive_stamp(state.cooked) == stamps
            print(f"PASS frozen fixture byte-exact Restore independent of pair {pair}", flush=True)

        if args.live_active:
            live = GameState(args.live_active.resolve(strict=True))
            tx = FontTransaction(live)
            before = snapshot(live.cooked, include_state=True, live=True)
            archive_before = archive_stamp(live.cooked)
            active = tx.load()
            assert active and active["status"] == "active"
            for name, entry in active["files"].items():
                assert tx.current(name) == entry["after"]
                assert live.get_object(entry["before"]) == (args.fixture / "copy/CookedPC" / name).read_bytes()
            for pair in ("PL-JP", "JP-PL"):
                result = root / ("active-" + pair + ".json")
                assert invoke(exe, "prepare", pair, live.cooked.parent, result) == 1
                assert "P2/P3 install or recovery is active" in report_at(result)["error"]
                print(f"PASS frozen selected installation read-only preflight blocks old active P2 {pair}", flush=True)
            assert snapshot(live.cooked, include_state=True, live=True) == before
            assert archive_stamp(live.cooked) == archive_before
            print(f"Selected installation unchanged ({len(before)} file digests, {len(archive_before)} archive stamps).",
                  flush=True)


if __name__ == "__main__":
    main()
