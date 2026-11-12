"""Headless full-corpus tests on legally obtained resource copies."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_p0 import GIBBED
from w2_codec import decode_source, inspect_table, plan_merge, transform_text
from w2_font_test import (FontTransaction, missing_codes, prepare_corpus, profile_files, read_catalog,
                          record_codes, run_probe)
from w2_install import GameState, StateError, digest
from w2_swf import Bits, has_outline, ink_bounds, library_faces, read_movie, tagged, write_movie, write_shape

ROOT = Path(__file__).resolve().parents[1]
PAIRS = (("PL", "JP"), ("JP", "PL"))


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["W2_P2_FIXTURE"])
        cls.stock = cls.root / "copy/CookedPC"
        cls.assets = cls.root / "assets"
        cls.catalog = read_catalog(ROOT / "resources/donor.json")
        cls.results = {}
        cls.decoded = {}
        with tempfile.TemporaryDirectory() as temp:
            for lang in ("PL", "JP"):
                loc, _ = profile_files(lang)
                cls.decoded[lang] = decode_source((cls.stock / loc).read_bytes(), Path(loc).stem, GIBBED, Path(temp))
            for pair in PAIRS:
                generated = prepare_corpus(cls.stock, cls.assets, pair, GIBBED, Path(temp), cls.catalog)
                cls.results[pair] = generated
                if folder := os.environ.get("W2_P2_REPORTS"):
                    report = Path(folder) / ("-".join(pair) + "_COVERAGE.json")
                    report.write_text(json.dumps(generated[2], ensure_ascii=False, indent=2) + "\n", "utf-8")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-p2t2-")
        self.addCleanup(temp.cleanup)
        self.game = Path(temp.name) / "fixture"
        self.cooked = self.game / "CookedPC"
        shutil.copytree(self.stock, self.cooked, ignore=shutil.ignore_patterns("_ExiZz_DualSubtitles_State"))
        self.state = GameState(self.game)
        with self.state.locked():
            slots = {name: {"original": self.state.remember((self.cooked / name).read_bytes()), "result": None}
                     for name in (profile_files(lang)[0] for lang in ("PL", "JP"))}
            self.state.save({**self.state.state, "slots": slots})

    def test_full_record_accounting_primary_order_and_binary_preservation(self):
        for pair, (outputs, _, report) in self.results.items():
            with self.subTest(pair=pair):
                left, right = (self.decoded[lang] for lang in pair)
                plan = plan_merge(left, right, left)
                loc, _ = profile_files(pair[0])
                final = inspect_table(outputs[loc], Path(loc).stem)
                changed = set(report["changed_ids"])
                skipped = {entry["id"]: set(entry["codes"]) for entry in report["skipped_glyph_records"]}
                self.assertEqual(changed | skipped.keys(), set(plan.changes))
                self.assertFalse(changed & skipped.keys())
                self.assertGreaterEqual(len(changed), 59900)
                self.assertEqual(final.header, left.table.header)
                self.assertEqual(tuple(final.records), tuple(left.table.records))
                self.assertEqual(final.keys, left.table.keys)
                for index, record in final.records.items():
                    if index in changed:
                        self.assertEqual(transform_text(record.encrypted, final.mask).decode("utf-16le"),
                                         plan.changes[index])
                    else:
                        self.assertEqual(record.encoded, left.table.records[index].encoded)
                for index, codes in skipped.items():
                    self.assertEqual(codes, record_codes(plan.changes[index]) & report["uncovered_codes"].keys())
                self.assertIn(115634, skipped)
                self.assertNotIn(79059, skipped)
                index = final.keys["menunew"]
                self.assertEqual(transform_text(final.records[index].encrypted, final.mask).decode("utf-16le"),
                                 {"PL": "NOWA GRA ~ ニューゲーム", "JP": "ニューゲーム ~ NOWA GRA"}[pair[0]])

    def test_font_ids_glyph_coverage_metrics_zones_and_exact_donors(self):
        for pair, (outputs, _, report) in self.results.items():
            with self.subTest(pair=pair):
                _, name = profile_files(pair[0])
                original = read_movie((self.stock / name).read_bytes())
                patched = read_movie(outputs[name])
                self.assertEqual((original.signature, original.version, original.frame, original.padded),
                                 (patched.signature, patched.version, patched.frame, patched.padded))
                self.assertEqual([tag for tag in original.tags if tag.kind not in (73, 75)],
                                 [tag for tag in patched.tags if tag.kind not in (73, 75)])
                self.assertEqual(missing_codes(outputs[name], report["required_codes"]), set())
                before, old_zones = library_faces(original)
                after, new_zones = library_faces(patched)
                self.assertEqual(before.keys(), after.keys())
                _, secondary = profile_files(pair[1])
                source_faces, _ = library_faces(read_movie((self.stock / secondary).read_bytes()))
                stock = source_faces[report["stock_donor"]["font_id"]][1]
                for face in report["faces"]:
                    ident = face["font_id"]
                    old, new = before[ident][1], after[ident][1]
                    self.assertEqual((new.ident, new.name, new.global_metrics, new.kerning),
                                     (old.ident, old.name, old.global_metrics, old.kerning))
                    self.assertEqual(len(new.glyphs), face["original"] + len(face["added"]))
                    self.assertTrue(face["stock_donor_codes"])
                    for cp, glyph in old.glyphs.items():
                        if cp in face["filled"]:
                            self.assertFalse(has_outline(glyph.shape))
                        else:
                            self.assertEqual(new.glyphs[cp], glyph)
                            self.assertEqual(new_zones[ident][1][cp], old_zones[ident][1][cp])
                    for cp in face["stock_donor_codes"]:
                        self.assertEqual(new.glyphs[cp], stock.glyphs[cp])
                    for cp in face["ofl_fallback_codes"]:
                        self.assertEqual(new.glyphs[cp], self.catalog[cp])

    def test_inplace_corpus_apply_conflicts_and_exact_restore_both_ways(self):
        paths = [path for path in self.cooked.rglob("*") if path.is_file()
                 and "_ExiZz_DualSubtitles_State" not in path.parts]
        originals = {path: digest(path.read_bytes()) for path in paths}
        with self.state.locked(), patch("w2_font_test.ensure_game_closed"):
            tx = FontTransaction(self.state)
            for pair in PAIRS:
                outputs, _, expected = self.results[pair]
                result = run_probe(self.state, pair, GIBBED.parent, apply=True)
                self.assertEqual(result["changed_ids"], expected["changed_ids"])
                self.assertEqual(result["skipped_glyph_records"], expected["skipped_glyph_records"])
                active = tx.load()
                self.assertEqual(set(active["files"]), set(outputs))
                for name, entry in active["files"].items():
                    self.assertEqual(entry["before"], originals[self.cooked / name])
                    self.assertEqual(entry["after"], digest(outputs[name]))
                    self.assertEqual((self.cooked / name).read_bytes(), outputs[name])
                other = tuple(reversed(pair))
                for attempt in (pair, other):
                    with self.assertRaisesRegex(StateError, "P2/P3 install or recovery is active"):
                        run_probe(self.state, attempt, GIBBED.parent, apply=True)
                self.assertEqual(tx.load(), active)
                self.assertTrue(tx.restore())
                self.assertEqual({path: digest(path.read_bytes()) for path in paths}, originals)

    def test_modified_primary_and_donor_revision_with_altered_font_ids(self):
        def revision(name, new_id):
            movie = read_movie((self.cooked / name).read_bytes())
            tags = []
            for tag in movie.tags:
                data = tag.data
                if tag.kind in (73, 75, 88) and data[:2] == b"\x01\x00":
                    tag = tagged(tag.kind, new_id + data[2:])
                elif tag.kind == 37:
                    bits = Bits(data)
                    bits.take(16)
                    bits.rect()
                    if bits.take(16) & 0x0100:
                        offset = bits.pos // 8
                        if data[offset:offset + 2] == b"\x01\x00":
                            tag = tagged(tag.kind, data[:offset] + new_id + data[offset + 2:])
                tags.append(tag)
            return write_movie(replace(movie, signature=b"FWS", padded=False, tags=tuple(tags)))

        custom = self.game.parent / "custom"
        shutil.copytree(self.assets, custom, ignore=shutil.ignore_patterns("*.w2scene"))
        for lang, ident in (("PL", 77), ("JP", 92)):
            _, relative = profile_files(lang)
            original = revision(relative, ident.to_bytes(2, "little"))
            (self.cooked / relative).write_bytes(original)
            (custom / relative).write_bytes(original)
        archive = self.cooked / "pack0.dzip"
        archive.unlink()
        subprocess.run([str(GIBBED.parent / "Gibbed.RED.Pack.exe"), str(archive), str(custom)],
                       capture_output=True, timeout=40, check=True)
        with self.state.locked(), patch("w2_font_test.ensure_game_closed"):
            report = run_probe(self.state, ("PL", "JP"), GIBBED.parent, apply=True)
            self.assertEqual({face["font_id"] for face in report["faces"]}, {3, 77})
            self.assertEqual(report["stock_donor"]["font_id"], 92)
            self.assertEqual(report["changed_ids"], self.results[("PL", "JP")][2]["changed_ids"])
            self.assertTrue(FontTransaction(self.state).restore())
            for lang in ("PL", "JP"):
                _, relative = profile_files(lang)
                self.assertEqual((self.cooked / relative).read_bytes(), (custom / relative).read_bytes())

    def test_donor_change_between_preparation_and_commit_rejected(self):
        pair = ("PL", "JP")
        outputs, guards, _ = self.results[pair]
        _, donor = profile_files("JP")
        source = self.cooked / donor
        source.write_bytes(source.read_bytes() + b"x")
        with self.state.locked():
            tx = FontTransaction(self.state)
            with self.assertRaisesRegex(StateError, "changed during preparation"):
                tx.apply(pair, outputs, guards)
            self.assertIsNone(tx.load())
            for name in outputs:
                self.assertEqual((self.cooked / name).read_bytes(), (self.stock / name).read_bytes())

    def test_actual_curve_bounds_are_independent_of_stored_rect(self):
        shape = write_shape([[(0, 0), ((100, -200), (200, 0)), ((0, 0),)]])
        self.assertEqual(ink_bounds(shape), (0, 200, -100, 0))

    def test_markup_does_not_drop_literal_angles_entities_or_special_unicode(self):
        text = '<b>猫</b><imię> [PL] &#x7B1E; &#x20000; e\u0301\u200f\ufe00'
        self.assertTrue({0x732B, 0x0119, 0x005B, 0x7B1E, 0x20000, 0x0301, 0x200F, 0xFE00}
                        <= record_codes(text))
        self.assertNotIn(ord("b"), record_codes("<b>猫</b>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
