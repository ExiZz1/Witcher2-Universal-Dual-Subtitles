from concurrent.futures import ProcessPoolExecutor, as_completed
from hashlib import sha256
from itertools import permutations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from w2_codec import inspect_table
from w2_font_test import FontTransaction, PROFILES, prepare_corpus, profile_files, read_catalog
from w2_install import GameState, StateError, digest
from w2_swf import library_faces, read_movie


def run_group(primary, strings, assets, results):
    outcomes = []
    catalog = read_catalog(SRC / "resources/donor.json")
    with tempfile.TemporaryDirectory(prefix="w2p3-") as temp:
        game = Path(temp)
        cooked = game / "CookedPC"
        cooked.mkdir()
        paths = set()
        for lang in PROFILES:
            slot, font = profile_files(lang)
            shutil.copyfile(strings / slot, cooked / slot)
            paths.update((slot, font))
            path = cooked / font
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(assets / font, path)
        originals = {name: digest((cooked / name).read_bytes()) for name in paths}
        state = GameState(game)
        with state.locked():
            slots = {name: {"original": state.remember((cooked / name).read_bytes()), "result": None}
                     for name in paths if name.endswith(".w2strings")}
            state.save({**state.state, "slots": slots})
            tx = FontTransaction(state)
            for secondary in sorted(PROFILES.keys() - {primary}):
                pair = primary, secondary
                label = "-".join(pair)
                try:
                    work = game / "temp"
                    work.mkdir(exist_ok=True)
                    outputs, guards, report = prepare_corpus(cooked, assets, pair,
                        SRC / "Gibbed RED Tools/Gibbed.RED.Strings.exe", work, catalog)
                    loc, font = profile_files(primary)
                    result = inspect_table(outputs[loc], Path(loc).stem)
                    assert report["p1_eligible"] == len(report["changed_ids"]) + len(report["skipped_glyph_records"])
                    assert not set(report["changed_ids"]) & {entry["id"] for entry in report["skipped_glyph_records"]}
                    assert len(result.records) == report["primary_records"]
                    assert all(len(face.glyphs) <= 65535
                               for _, face in library_faces(read_movie(outputs[font]))[0].values())
                    tx.apply(pair, outputs, guards)
                    assert {name for name in paths if tx.current(name) != originals[name]} == set(tx.load()["files"])
                    try:
                        tx.apply(pair, outputs, guards)
                    except StateError as error:
                        assert "Restore it first" in str(error)
                    else:
                        raise AssertionError("Active pair accepted reapplication")
                    assert tx.restore() and not tx.load()
                    assert all(tx.current(name) == key for name, key in originals.items())
                    changed = report.pop("changed_ids")
                    report["changed_count"] = len(changed)
                    report["changed_ids_sha256"] = sha256(",".join(map(str, changed)).encode("ascii")).hexdigest()
                    with (results / (label + ".json")).open("w", encoding="utf-8") as handle:
                        json.dump(report, handle, ensure_ascii=False, separators=(",", ":"))
                    row = {"pair": label, "primary_slot": loc, "target_font": font,
                           "status": "verified_offline",
                           "rendering": ("prior version user-confirmed" if set(pair) in ({"PL", "JP"}, {"TR", "JP"})
                                         else "manual test pending"),
                           "candidates": report["p1_eligible"], "written": len(changed),
                           "glyph_exceptions": len(report["skipped_glyph_records"]),
                           "unsupported_codes": len(report["uncovered_codes"]), "glyphs_covered": len(report["required_codes"]),
                           "stock_imports_by_font": [len(face["stock_donor_codes"]) for face in report["faces"]],
                           "ofl_imports_by_font": [len(face["ofl_fallback_codes"]) for face in report["faces"]],
                           "zh_primary_adjusted_by_font": [len(face["adjusted_existing"]) for face in report["faces"]],
                           "zh_primary_retained_by_font": [len(face["zh_primary_stock_kept"]) for face in report["faces"]],
                           "zh_adjusted_by_font": [len(face["zh_adjusted_stock_codes"]) for face in report["faces"]],
                           "zh_stock_kept_by_font": [len(face["zh_stock_kept_by_safety_check"]) for face in report["faces"]],
                           "localization_sha256": digest(outputs[loc]), "font_sha256": digest(outputs[font]),
                           "shared_han_different_outlines": len(report["shared_han_different_outlines_primary_retained"]),
                           "font_original_bytes": report["font_size"]["original_bytes"],
                           "font_patched_bytes": report["font_size"]["patched_bytes"],
                           "longest_utf16": report["longest_merged_utf16_units"],
                           "prepared_seconds": report["build_seconds"],
                           "install_restore": "byte exact; repeat blocked while active"}
                except Exception as error:
                    if tx.load():
                        tx.restore()
                    row = {"pair": label, "status": "blocked", "error": str(error)}
                outcomes.append(row)
                print(label, row["status"], row.get("written"), row.get("glyph_exceptions"), row.get("error", ""), flush=True)
    return outcomes


def main():
    parser = argparse.ArgumentParser(description="Full Primary-slot font matrix on local copies")
    parser.add_argument("strings", type=Path)
    parser.add_argument("assets", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    reports = args.results.resolve() / "Coverage"
    reports.mkdir(parents=True, exist_ok=True)
    matrix = []
    with ProcessPoolExecutor(max_workers=args.workers) as workers:
        jobs = [workers.submit(run_group, primary, args.strings.resolve(), args.assets.resolve(), reports)
                for primary in sorted(PROFILES)]
        for job in as_completed(jobs):
            matrix.extend(job.result())
    matrix.sort(key=lambda row: row["pair"])
    assert {row["pair"] for row in matrix} == {"-".join(pair) for pair in permutations(PROFILES, 2)}
    (args.results / "P3_MATRIX.json").write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    return 1 if any(row["status"] != "verified_offline" for row in matrix) else 0


if __name__ == "__main__":
    raise SystemExit(main())
