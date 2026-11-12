"""Headless P2 integration tests on game-resource copies."""
from dataclasses import replace
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_p0 import APPROVE, GIBBED
from w2_codec import inspect_table, transform_text
from w2_font_test import (FontTransaction, installed_game, missing_codes, prepare_control as prepare, profile_files,
                          read_catalog, run_probe)
from w2_install import GameState, Pair, StateError, digest
from w2_swf import FontError, has_outline, library_faces, patch_library, read_movie, tagged, write_movie

ROOT = Path(__file__).resolve().parents[1]
FONT = "globals/gui/fonts.swf"


class P2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = os.environ.get("W2_P2_FIXTURE")
        if not fixture:
            raise unittest.SkipTest("Set W2_P2_FIXTURE to the documented real-resource fixture.")
        cls.fixture = Path(fixture)
        cls.stock = cls.fixture / "copy/CookedPC"
        cls.assets = cls.fixture / "assets"
        cls.donor = read_catalog(ROOT / "resources/donor.json")
        cls.generated = {}
        for pair in (("PL", "JP"), ("JP", "PL")):
            with tempfile.TemporaryDirectory() as tmp:
                cls.generated[pair] = prepare(cls.stock, cls.assets, pair, GIBBED, Path(tmp), cls.donor)

    def setUp(self):
        short = Path.home() / "AppData/Local/Temp"
        temp = tempfile.TemporaryDirectory(dir=short if short.is_dir() else None)
        self.addCleanup(temp.cleanup)
        self.game = Path(temp.name) / "copy"
        self.cooked = self.game / "CookedPC"
        shutil.copytree(self.stock, self.cooked, ignore=shutil.ignore_patterns("_ExiZz_DualSubtitles_State"))
        state = GameState(self.game)
        with state.locked():
            slots = {name: {"original": state.remember((self.cooked / name).read_bytes()), "result": None}
                     for name in ("pl0.w2strings", "jp0.w2strings")}
            state.save({**state.state, "slots": slots})

    def test_both_primary_profiles_and_exact_restore(self):
        for pair, (outputs, guards, report) in self.generated.items():
            with self.subTest(pair=pair):
                self.assertEqual(len(report["changed_ids"]), 32)
                self.assertEqual(len(report["required_codes"]), 222)
                self.assertFalse(report["skipped_control_ids"])
                local, font = profile_files(pair[0])
                original = {name: (self.cooked / name).read_bytes() for name in outputs}
                base = inspect_table(original[local], Path(local).stem)
                after = inspect_table(outputs[local], Path(local).stem)
                self.assertEqual(base.header, after.header)
                self.assertEqual(tuple(base.records), tuple(after.records))
                changed = {i for i in base.records if base.records[i] != after.records[i]}
                self.assertEqual(changed, set(report["changed_ids"]))
                for index in changed:
                    text = transform_text(after.records[index].encrypted, after.mask).decode("utf-16le")
                    first = transform_text(base.records[index].encrypted, base.mask).decode("utf-16le")
                    self.assertTrue(text.startswith(first + " ~ "))
                self.assertEqual(missing_codes(outputs[font], report["required_codes"]), set())
                state = GameState(self.game)
                with state.locked():
                    tx = FontTransaction(state)
                    tx.apply(pair, outputs, guards)
                    for name, data in outputs.items():
                        self.assertEqual((self.cooked / name).read_bytes(), data)
                    with self.assertRaisesRegex(StateError, "P2"):
                        state.install(Pair.select("Polish", "Japanese"), GIBBED, APPROVE)
                    with self.assertRaisesRegex(StateError, "P2"):
                        state.restore(APPROVE)
                    self.assertTrue(tx.restore())
                    self.assertFalse(tx.restore())
                for name, data in original.items():
                    self.assertEqual((self.cooked / name).read_bytes(), data)

    def test_all_original_metrics_and_alignment_records_preserved(self):
        for pair, (outputs, _, report) in self.generated.items():
            _, relative = profile_files(pair[0])
            base = read_movie((self.stock / relative).read_bytes())
            new = read_movie(outputs[relative])
            self.assertEqual((base.signature, base.version, base.frame, base.padded),
                             (new.signature, new.version, new.frame, new.padded))
            self.assertEqual([t for t in base.tags if t.kind not in (73, 75)],
                             [t for t in new.tags if t.kind not in (73, 75)])
            original_faces, original_zones = library_faces(base)
            revised_faces, revised_zones = library_faces(new)
            self.assertEqual(original_faces.keys(), revised_faces.keys())
            for face_result in report["faces"]:
                ident = face_result["font_id"]
                before = original_faces[ident][1]
                after = revised_faces[ident][1]
                filled = set(face_result["filled"])
                self.assertEqual(after.global_metrics, before.global_metrics)
                self.assertEqual(after.kerning, before.kerning)
                self.assertEqual(after.prefix, before.prefix)
                for cp, glyph in before.glyphs.items():
                    if cp not in filled:
                        self.assertEqual(after.glyphs[cp], glyph)
                        self.assertEqual(original_zones[ident][1][cp], revised_zones[ident][1][cp])
                    else:
                        self.assertFalse(has_outline(glyph.shape))
                        self.assertTrue(has_outline(after.glyphs[cp].shape))

    def test_structurally_compatible_fws_with_different_font_id(self):
        base = read_movie((self.stock / FONT).read_bytes())
        tags = []
        for tag in base.tags:
            data = tag.data
            if tag.kind in (75, 73, 88) and data[:2] == b"\x01\x00":
                tag = tagged(tag.kind, b"\x4d\x00" + data[2:])
            elif tag.kind == 37:
                from w2_swf import Bits
                bits = Bits(data)
                bits.take(16)
                bits.rect()
                if bits.take(16) & 0x0100:
                    pos = bits.pos // 8
                    if data[pos:pos + 2] == b"\x01\x00":
                        tag = tagged(tag.kind, data[:pos] + b"\x4d\x00" + data[pos + 2:])
            tags.append(tag)
        alternate = write_movie(replace(base, signature=b"FWS", padded=False, tags=tuple(tags)))
        names = {"Bell Gothic", "Bell Gothic Black"}
        required = self.generated[("PL", "JP")][2]["required_codes"]
        output, report = patch_library(alternate, names, required, self.donor)
        self.assertEqual({row["font_id"] for row in report}, {3, 77})
        self.assertEqual(read_movie(output).signature, b"FWS")
        self.assertEqual(missing_codes(output, required), set())

    def test_unknown_or_index_dependent_swf_rejected(self):
        base = read_movie((self.stock / FONT).read_bytes())
        names = {"Bell Gothic", "Bell Gothic Black"}
        for content in (
            b"ZWS" + write_movie(base)[3:],
            write_movie(replace(base, tags=(*base.tags[:-1], tagged(11, b""), base.tags[-1]))),
            write_movie(base)[:-12] + b"not-zero-pad",
        ):
            with self.assertRaises((FontError, ValueError)):
                patch_library(content, names, {12354}, self.donor)
        with self.assertRaisesRegex(FontError, "BMP"):
            patch_library(write_movie(base), names, {0x20000}, self.donor)
        with self.assertRaisesRegex(FontError, "donor"):
            patch_library(write_movie(base), names, {12354}, {})

    def test_created_loose_font_removed_by_restore(self):
        path = self.cooked / FONT
        path.unlink()
        with tempfile.TemporaryDirectory() as tmp:
            outputs, guards, _ = prepare(self.cooked, self.assets, ("PL", "JP"), GIBBED, Path(tmp), self.donor)
        self.assertIsNone(guards[FONT])
        state = GameState(self.game)
        with state.locked():
            tx = FontTransaction(state)
            tx.apply(("PL", "JP"), outputs, guards)
            self.assertTrue(path.is_file())
            self.assertTrue(tx.restore())
            self.assertFalse(path.exists())

    def test_external_font_change_before_apply_and_after_install(self):
        outputs, guards, _ = self.generated[("PL", "JP")]
        font = self.cooked / FONT
        original = font.read_bytes()
        stock_text = (self.cooked / "pl0.w2strings").read_bytes()
        state = GameState(self.game)
        with state.locked():
            tx = FontTransaction(state)
            font.write_bytes(b"external")
            with self.assertRaisesRegex(StateError, "changed"):
                tx.apply(("PL", "JP"), outputs, guards)
            self.assertFalse(tx.journal.exists())
            self.assertEqual((self.cooked / "pl0.w2strings").read_bytes(), stock_text)
            font.write_bytes(original)
            tx.apply(("PL", "JP"), outputs, guards)
            font.write_bytes(b"external")
            with self.assertRaisesRegex(StateError, "External"):
                tx.restore()
            self.assertEqual(font.read_bytes(), b"external")
            self.assertEqual((self.cooked / "pl0.w2strings").read_bytes(), outputs["pl0.w2strings"])

    def test_second_resource_failure_restores_first_resource(self):
        outputs, guards, _ = self.generated[("PL", "JP")]
        state = GameState(self.game)
        original = {name: (self.cooked / name).read_bytes() for name in outputs}
        with state.locked():
            tx = FontTransaction(state)
            actual = tx.switch

            def fail_font(name, before, after):
                if name == FONT and before != after:
                    raise OSError("injected font write error")
                actual(name, before, after)

            with patch.object(tx, "switch", side_effect=fail_font), self.assertRaisesRegex(OSError, "font write"):
                tx.apply(("PL", "JP"), outputs, guards)
            self.assertFalse(tx.journal.exists())
        self.assertEqual({name: (self.cooked / name).read_bytes() for name in outputs}, original)

    def test_interrupted_transaction_can_restore_from_new_process(self):
        outputs, guards, _ = self.generated[("PL", "JP")]
        script = """
from pathlib import Path
import os, sys, tempfile
from w2_install import GameState
from w2_font_test import FontTransaction, prepare_control, read_catalog
game, extracted, gibbed = map(Path, sys.argv[1:])
state = GameState(game)
with state.locked(), tempfile.TemporaryDirectory() as tmp:
    pair = ("PL", "JP")
    outputs, guards, _ = prepare_control(state.cooked, extracted, pair, gibbed, Path(tmp),
                                  read_catalog(Path("resources/donor.json")))
    tx = FontTransaction(state)
    real = tx.switch
    def terminate(name, before, after):
        real(name, before, after)
        os._exit(73)
    tx.switch = terminate
    tx.apply(pair, outputs, guards)
"""
        command = subprocess.run([sys.executable, "-c", script, str(self.game), str(self.assets), str(GIBBED)],
                                 cwd=ROOT, timeout=90, capture_output=True)
        self.assertEqual(command.returncode, 73, command.stderr.decode(errors="replace"))
        self.assertEqual((self.cooked / "pl0.w2strings").read_bytes(), outputs["pl0.w2strings"])
        state = GameState(self.game)
        with state.locked():
            self.assertEqual(FontTransaction(state).load()["status"], "applying")
            self.assertTrue(FontTransaction(state).restore())
        for name in outputs:
            self.assertEqual(digest((self.cooked / name).read_bytes()), guards[name])

    def test_accepts_selected_installation_and_rejects_missing_cookedpc(self):
        self.assertEqual(installed_game(self.game), self.game)
        with self.assertRaisesRegex(StateError, "containing CookedPC"):
            installed_game(self.cooked)

    def test_refuses_missing_original_and_modified_localization(self):
        state = GameState(self.game)
        with state.locked():
            tx = FontTransaction(state)
            slots = state.state["slots"].copy()
            state.state["slots"] = {}
            with self.assertRaisesRegex(StateError, "No verified"):
                tx.preflight(("PL", "JP"))
            state.state["slots"] = slots
            (self.cooked / "jp0.w2strings").write_bytes(b"unknown edit")
            with self.assertRaisesRegex(StateError, "recorded original"):
                tx.preflight(("PL", "JP"))
            self.assertFalse(tx.journal.exists())

    def test_refuses_active_or_interrupted_regular_install(self):
        state = GameState(self.game)
        with state.locked():
            tx = FontTransaction(state)
            state.state["pending"] = {"files": []}
            with self.assertRaisesRegex(StateError, "P0/P1"):
                tx.preflight(("PL", "JP"))
            state.state["pending"] = None
            record = state.state["slots"]["pl0.w2strings"]
            record["result"] = record["original"]
            with self.assertRaisesRegex(StateError, "P0/P1"):
                tx.preflight(("PL", "JP"))
            self.assertFalse(tx.journal.exists())

    def test_refuses_legacy_font_override_and_legacy_localization_mismatch(self):
        state = GameState(self.game)
        legacy = self.cooked / "_DualSubtitles_Backups"
        legacy.mkdir()
        with state.locked():
            tx = FontTransaction(state)
            (legacy / "pl0.w2strings.original").write_bytes(b"foreign")
            with self.assertRaisesRegex(StateError, "Conflicting legacy"):
                tx.preflight(("PL", "JP"))
            (legacy / "pl0.w2strings.original").unlink()
            (legacy / "_cjk_assets.json").write_text(json.dumps([
                {"rel": "globals/gui/cfonts.swf", "had_original": False}
            ]), encoding="utf-8")
            (self.cooked / "globals/gui/cfonts.swf").write_bytes(b"foreign")
            with self.assertRaisesRegex(StateError, "legacy CJK"):
                tx.preflight(("PL", "JP"))

    def test_unmodified_font_not_backed_up_or_journaled(self):
        outputs, guards, _ = self.generated[("PL", "JP")]
        outputs = {**outputs, FONT: (self.cooked / FONT).read_bytes()}
        state = GameState(self.game)
        with state.locked():
            tx = FontTransaction(state)
            tx.apply(("PL", "JP"), outputs, guards)
            self.assertEqual(set(tx.load()["files"]), {"pl0.w2strings"})
            self.assertFalse((state.objects / guards[FONT]).exists())
            self.assertTrue(tx.restore())

    def test_apply_to_selected_folder_under_steamapps_with_no_copy_flag(self):
        selected = self.game.parent / "steamapps/common/Witcher 2"
        selected.parent.mkdir(parents=True)
        self.game.rename(selected)
        state = GameState(selected)
        with state.locked():
            with patch("w2_font_test.ensure_game_closed"):
                report = run_probe(state, ("PL", "JP"), GIBBED.parent, apply=True, control=True)
            self.assertEqual(len(report["changed_ids"]), 32)
            self.assertTrue(report["applied"])
            self.assertTrue(FontTransaction(state).restore())

    def test_untracked_loose_override_rejected_before_writing(self):
        font = self.cooked / FONT
        original = read_movie(font.read_bytes())
        unknown = write_movie(replace(original, frame=original.frame[:-4] + b"\0\x01" + original.frame[-2:]))
        self.assertNotEqual(font.read_bytes(), unknown)
        font.write_bytes(unknown)
        state = GameState(self.game)
        with state.locked():
            with self.assertRaisesRegex(StateError, "Unknown loose override"):
                run_probe(state, ("PL", "JP"), GIBBED.parent, apply=True)
            self.assertFalse(FontTransaction(state).journal.exists())
        self.assertEqual(font.read_bytes(), unknown)

    def test_unknown_archive_index_blocks_before_writing(self):
        (self.cooked / "unknown.dzip").write_bytes(b"bad archive")
        state = GameState(self.game)
        with state.locked():
            with self.assertRaisesRegex(StateError, "verify resource overlays"):
                run_probe(state, ("PL", "JP"), GIBBED.parent, apply=True)
            self.assertFalse(FontTransaction(state).journal.exists())

    def test_structurally_valid_but_conflicting_archive_font_is_refused(self):
        root = self.game.parent / "mod_resources"
        font = root / FONT
        font.parent.mkdir(parents=True)
        original = read_movie((self.cooked / FONT).read_bytes())
        foreign = write_movie(replace(original, frame=original.frame[:-4] + bytes((0, 1)) + original.frame[-2:]))
        font.write_bytes(foreign)
        archive = self.cooked / "third-party.dzip"
        packed = subprocess.run([str(GIBBED.parent / "Gibbed.RED.Pack.exe"), str(archive), str(root)],
                                capture_output=True, timeout=30)
        self.assertEqual(packed.returncode, 0, packed.stderr)
        state = GameState(self.game)
        with state.locked():
            with self.assertRaisesRegex(StateError, "Conflicting resource .* in third-party.dzip"):
                run_probe(state, ("PL", "JP"), GIBBED.parent, apply=True)
            self.assertFalse(FontTransaction(state).journal.exists())
        self.assertEqual((self.cooked / FONT).read_bytes(), (self.stock / FONT).read_bytes())

    def test_install_button_uses_primary_font_route_without_legacy_modal(self):
        from Witcher2_Dual_Subtitles_Tool import App
        for primary, secondary, expected in (
            ("Polish", "Japanese", ("PL", "JP")),
            ("Japanese", "Chinese", ("JP", "ZH")),
            ("Turkish", "Japanese", ("TR", "JP")),
            ("English", "Polish", ("EN", "PL")),
        ):
            called = []
            app = SimpleNamespace(game=SimpleNamespace(get=lambda: str(self.game)),
                                  gibbed=SimpleNamespace(get=lambda: str(GIBBED)), busy=False,
                                  primary_lang=SimpleNamespace(get=lambda: primary),
                                  secondary_lang=SimpleNamespace(get=lambda: secondary),
                                  run_operation=lambda *args: called.append(args))
            with patch("Witcher2_Dual_Subtitles_Tool.messagebox.askyesnocancel") as modal:
                App.start_install(app)
            modal.assert_not_called()
            self.assertEqual(called[0][1:3], ("install", expected))


if __name__ == "__main__":
    unittest.main(verbosity=2)

