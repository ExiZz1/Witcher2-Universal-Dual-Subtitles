"""Compare the Chinese stock donor and the fitted glyphs on authentic resource copies."""
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
from statistics import median
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from w2_font_test import FontTransaction, is_han, run_probe
from w2_install import GameState
from smoke_p3 import build_fixture, pack_fixture, snapshot
from w2_swf import Bits, Glyph, has_outline, ink_bounds, library_faces, read_movie, rect_bytes, shape_geometry, tagged, write_movie
from w2_zh_ink import contour_area, improve, primary_side_limits

SAMPLE = "一二三永國語龍體"
ASSETS = Path(os.environ["W2_P3_ASSETS"])


def read_faces(name):
    movie = read_movie((ASSETS / "globals/gui" / name).read_bytes())
    return [face for _, face in library_faces(movie)[0].values()]


def metrics(glyph):
    left, right, top, bottom = ink_bounds(glyph.shape)
    area = abs(sum(contour_area(contour) for contour in shape_geometry(glyph.shape))) / 6
    return {"width": right - left, "height": bottom - top, "top": top, "bottom": bottom,
            "advance": glyph.advance, "ink_area": round(area)}


class ChineseInkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh, = read_faces("fonts_zh.swf")
        cls.jp, = read_faces("fonts_jp.swf")
        cls.bell = read_faces("fonts.swf")
        cls.ascent = min(struct.unpack("<hhh", face.global_metrics)[0] for face in cls.bell)
        cls.descent = min(struct.unpack("<hhh", face.global_metrics)[1] for face in cls.bell)

    def test_simple_han_stroke_is_larger_with_same_advance(self):
        stock = self.zh.glyphs[ord("一")]
        adjusted, reason = improve(stock, self.ascent, self.descent)
        self.assertIsNone(reason)
        self.assertEqual(adjusted.advance, stock.advance)
        self.assertGreater(metrics(adjusted)["height"], metrics(stock)["height"])
        self.assertGreater(metrics(adjusted)["ink_area"], metrics(stock)["ink_area"])
        self.assertEqual(Bits(adjusted.bounds).rect(), ink_bounds(adjusted.shape))

    def test_strict_fill_rejects_unsupported_styles(self):
        stock = self.zh.glyphs[ord("一")]
        other = Glyph(bytes([stock.shape[0] | 1]) + stock.shape[1:], stock.advance, stock.bounds)
        self.assertIn("unsupported donor fill", improve(other, self.ascent, self.descent)[1])

    def test_tight_cell_retains_original_without_distortion(self):
        stock = self.zh.glyphs[ord("國")]
        adjusted, reason = improve(stock, 800, 100)
        self.assertEqual(adjusted, stock)
        self.assertIn("line metrics", reason)

    def test_chinese_donor_revision_with_reassigned_font_id_and_fws(self):
        strings = Path(os.environ["W2_TEST_COPY_SOURCE"])
        with tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-zh-revision-") as name:
            game, cooked, selected = build_fixture(Path(name), strings, ASSETS)
            donor = selected / "globals/gui/fonts_zh.swf"
            movie = read_movie(donor.read_bytes())
            replacement = bytes((93, 0))
            tags = []
            for tag in movie.tags:
                data = tag.data
                if tag.kind in (73, 75, 88) and data[:2] == bytes((1, 0)):
                    tag = tagged(tag.kind, replacement + data[2:])
                elif tag.kind == 37:
                    bits = Bits(data)
                    bits.take(16)
                    bits.rect()
                    if bits.take(16) & 0x0100:
                        offset = bits.pos // 8
                        if data[offset:offset + 2] == bytes((1, 0)):
                            tag = tagged(tag.kind, data[:offset] + replacement + data[offset + 2:])
                tags.append(tag)
            donor.write_bytes(write_movie(replace(movie, signature=b"FWS", padded=False, tags=tuple(tags))))
            pack_fixture(selected, cooked / "pack0.dzip")
            original = snapshot(cooked)
            state = GameState(game)
            with state.locked(), patch("w2_font_test.ensure_game_closed"):
                report = run_probe(state, ("EN", "ZH"), ROOT / "Gibbed RED Tools",
                                   apply=True, approve=lambda _: True)
                self.assertEqual(report["stock_donor"]["font_id"], 93)
                self.assertTrue(all(face["zh_adjusted_stock_codes"] for face in report["faces"]))
                self.assertTrue(FontTransaction(state).restore())
                primary = run_probe(state, ("ZH", "EN"), ROOT / "Gibbed RED Tools",
                                    apply=True, approve=lambda _: True)
                self.assertEqual({face["font_id"] for face in primary["faces"]}, {93})
                self.assertTrue(primary["faces"][0]["adjusted_existing"])
                self.assertTrue(FontTransaction(state).restore())
            self.assertEqual(snapshot(cooked), original)

    def test_chinese_primary_install_changes_only_documented_outlines(self):
        strings = Path(os.environ["W2_TEST_COPY_SOURCE"])
        with tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-zh-primary-") as name:
            game, cooked, selected = build_fixture(Path(name), strings, ASSETS)
            relative = Path("globals/gui/fonts_zh.swf")
            original_movie = read_movie((selected / relative).read_bytes())
            original_faces, original_zones = library_faces(original_movie)
            original = snapshot(cooked)
            state = GameState(game)
            with state.locked(), patch("w2_font_test.ensure_game_closed"):
                report = run_probe(state, ("ZH", "EN"), ROOT / "Gibbed RED Tools",
                                   apply=True, approve=lambda _: True)
                new_movie = read_movie((cooked / relative).read_bytes())
                faces, zones = library_faces(new_movie)
                self.assertEqual(faces.keys(), original_faces.keys())
                self.assertEqual([tag for tag in original_movie.tags if tag.kind not in (73, 75)],
                                 [tag for tag in new_movie.tags if tag.kind not in (73, 75)])
                for row in report["faces"]:
                    ident = row["font_id"]
                    before, after = original_faces[ident][1], faces[ident][1]
                    self.assertEqual(after.global_metrics, before.global_metrics)
                    self.assertEqual(after.kerning, before.kerning)
                    self.assertEqual(after.prefix, before.prefix)
                    self.assertEqual(after.glyphs.keys() - before.glyphs.keys(), set(row["added"]))
                    self.assertTrue(before.glyphs.keys() <= after.glyphs.keys())
                    corrected = set(row["adjusted_existing"])
                    self.assertGreater(len(corrected), 1000)
                    self.assertTrue(all(is_han(cp) for cp in corrected))
                    for cp, glyph in before.glyphs.items():
                        updated = after.glyphs[cp]
                        if cp in corrected:
                            self.assertNotEqual(updated.shape, glyph.shape)
                            self.assertEqual(updated.advance, glyph.advance)
                            left, right, top, bottom = ink_bounds(updated.shape)
                            self.assertEqual(Bits(updated.bounds).rect(), (left, right, top, bottom))
                            low, high = primary_side_limits(glyph)
                            self.assertGreaterEqual(left, low)
                            self.assertLessEqual(right, high)
                            self.assertGreaterEqual(top, -16400)
                            self.assertLessEqual(bottom, 4080)
                            self.assertEqual(zones[ident][1][cp], bytes((2,)) + bytes(9))
                        elif cp not in row["filled"]:
                            self.assertEqual(updated, glyph)
                            self.assertEqual(zones[ident][1][cp], original_zones[ident][1][cp])
                self.assertTrue(FontTransaction(state).restore())
            self.assertEqual(snapshot(cooked), original)

    def test_primary_geometry_matches_reference_with_tighter_cells(self):
        report_folder = Path(os.environ["W2_ZH_REPORTS"])
        p3 = Path(os.environ["W2_MATRIX_RESULTS"])
        required = set(json.loads((p3 / "Coverage/EN-ZH.json").read_text("utf-8"))["faces"][0]["stock_donor_codes"])
        cohort = sorted(cp for cp in required if is_han(cp) and cp in self.jp.glyphs
                        and has_outline(self.jp.glyphs[cp].shape))
        ascent, descent, leading = struct.unpack("<hhh", self.zh.global_metrics)
        readings = {"stock_primary": [], "test2_secondary": [], "test3_primary": []}
        adjusted = 0
        for cp in cohort:
            glyph = self.zh.glyphs[cp]
            ref, _ = improve(glyph, self.ascent, self.descent)
            candidate, reason = improve(glyph, ascent, descent, primary=True)
            if reason is None:
                adjusted += 1
                self.assertEqual(candidate.advance, glyph.advance)
                lo, hi = primary_side_limits(glyph)
                left, right, top, bottom = ink_bounds(candidate.shape)
                self.assertTrue(lo <= left < right <= hi and -ascent <= top < bottom <= descent)
                self.assertTrue(all(contour_area(a) * contour_area(b) > 0 for a, b in zip(
                    shape_geometry(glyph.shape), shape_geometry(candidate.shape, strict_fill=True), strict=True)))
            else:
                self.assertEqual(candidate, glyph)
            for label, measured in zip(readings, (glyph, ref, candidate)):
                readings[label].append(metrics(measured))
        medians = {label: {key: median(row[key] for row in rows) for key in rows[0]}
                   for label, rows in readings.items()}
        self.assertGreater(adjusted, len(cohort) * 0.95)
        self.assertGreater(medians["test3_primary"]["height"], medians["stock_primary"]["height"])
        self.assertGreater(medians["test3_primary"]["ink_area"], medians["stock_primary"]["ink_area"])
        self.assertLess(abs(medians["test3_primary"]["height"] / medians["test2_secondary"]["height"] - 1), 0.05)
        self.assertLess(abs(medians["test3_primary"]["ink_area"] / medians["test2_secondary"]["ink_area"] - 1), 0.08)
        samples = {}
        for char in SAMPLE:
            g = self.zh.glyphs[ord(char)]
            primary, _ = improve(g, ascent, descent, primary=True)
            secondary, _ = improve(g, self.ascent, self.descent)
            samples[char] = {label: metrics(measured) for label, measured in
                             zip(readings, (g, secondary, primary))}
        report = {"cohort": len(cohort), "adjusted": adjusted, "medians": medians, "samples": samples,
                  "primary_global_metrics": [ascent, descent, leading],
                  "secondary_target_cell": [self.ascent, self.descent]}
        (report_folder / "ZH_PRIMARY_GEOMETRY.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")

    def test_compare_regions_and_verify_all_fitted_han(self):
        report_folder = Path(os.environ["W2_ZH_REPORTS"])
        p3 = Path(os.environ["W2_MATRIX_RESULTS"])
        required = set(json.loads((p3 / "Coverage/EN-ZH.json").read_text("utf-8"))["faces"][0]["stock_donor_codes"])
        required = {cp for cp in required if is_han(cp)}
        cohort = sorted(cp for cp in required if cp in self.jp.glyphs and has_outline(self.jp.glyphs[cp].shape))
        self.assertGreater(len(cohort), 1000)
        groups = {"stock_pmingliu": [], "fitted_pmingliu": [], "stock_yozfontm90": []}
        skipped = Counter()
        for cp in sorted(required):
            stock = self.zh.glyphs[cp]
            adjusted, reason = improve(stock, self.ascent, self.descent)
            if reason:
                self.assertEqual(adjusted, stock)
                skipped[reason] += 1
            else:
                self.assertEqual(adjusted.advance, stock.advance)
                before = shape_geometry(stock.shape)
                after = shape_geometry(adjusted.shape, strict_fill=True)
                self.assertEqual(len(before), len(after))
                self.assertTrue(all(contour_area(a) * contour_area(b) > 0 for a, b in zip(before, after)))
                bounds = Bits(adjusted.bounds).rect()
                self.assertEqual(bounds, ink_bounds(adjusted.shape))
                self.assertGreaterEqual(bounds[2], -self.ascent)
                self.assertLessEqual(bounds[3], self.descent)
            if cp in cohort:
                for group, glyph in zip(groups, (stock, adjusted, self.jp.glyphs[cp])):
                    groups[group].append(metrics(glyph))
        self.assertLess(sum(skipped.values()), len(required) // 20)
        medians = {group: {key: median(row[key] for row in rows) for key in rows[0]}
                   for group, rows in groups.items()}
        self.assertGreater(medians["fitted_pmingliu"]["height"], medians["stock_pmingliu"]["height"])
        self.assertGreater(medians["fitted_pmingliu"]["ink_area"], medians["stock_pmingliu"]["ink_area"])
        samples = {}
        for char in SAMPLE:
            cp = ord(char)
            z = self.zh.glyphs[cp]
            samples[char] = {label: metrics(glyph) for label, glyph in
                             zip(groups, (z, improve(z, self.ascent, self.descent)[0], self.jp.glyphs[cp]))}
        output = {"matched_han_cohort": len(cohort), "eligible_han": len(required),
                  "stock_retained": skipped, "medians": medians, "samples": samples,
                  "global_ascent_descent_leading": {face.name: struct.unpack("<hhh", face.global_metrics)
                                                    for face in (*self.bell, self.zh, self.jp)}}
        (report_folder / "ZH_GEOMETRY.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
