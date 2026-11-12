"""P1 merge and compatibility regressions. Uses synthetic copies plus optional P0 source."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from test_p0 import APPROVE, EN, GIBBED, JP, PAIR, PL, TR, fixture, verify_text
import w2_codec as codec
from w2_install import GameState, Pair


class P1Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="w2-p1-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cooked = self.root / "CookedPC"
        self.cooked.mkdir()

    def put(self, name, texts, keys=None):
        data = fixture(Path(name).stem, texts, keys)
        (self.cooked / name).write_bytes(data)
        return data

    def test_same_id_order_identical_and_empty_records_for_both_target_directions(self):
        original = {
            EN: self.put(EN, {1: "English ~ literal", 2: "Same", 3: "", 4: "only EN"}),
            JP: self.put(JP, {4: "", 3: "only JP", 2: "Same", 1: "日本語"}),
        }
        state = GameState(self.root)
        with state.locked():
            for names, expected in [
                (("English", "Japanese"), "English ~ literal ~ 日本語"),
                (("Japanese", "English"), "日本語 ~ English ~ literal"),
            ]:
                result = state.install(Pair.select(*names), GIBBED, APPROVE)
                self.assertEqual((result.changed, result.already, result.empty), (1, 1, 2))
                self.assertEqual(verify_text(result.data, JP, 1), expected)
                self.assertEqual(verify_text(result.data, JP, 2), "Same")
                self.assertEqual(verify_text(result.data, JP, 3), "only JP")
                self.assertEqual(verify_text(result.data, JP, 4), "")
            self.assertEqual(state.restore(APPROVE), (JP,))
        for name, data in original.items():
            self.assertEqual((self.cooked / name).read_bytes(), data)

    def test_named_id_can_move_when_both_tables_rule_out_collision(self):
        original = {
            EN: self.put(EN, {1: "Anchor", 2: "Anchor 2", 147136: "Drink elixirs"},
                         {"first": 1, "second": 2, "same_key": 147136}),
            PL: self.put(PL, {147138: "Pij eliksiry", 1: "Anchor", 2: "Anchor 2"},
                         {"first": 1, "second": 2, "same_key": 147138}),
        }
        state = GameState(self.root)
        with state.locked():
            for names, target, index, expected in [
                (("English", "Polish"), EN, 147136, "Drink elixirs ~ Pij eliksiry"),
                (("Polish", "English"), PL, 147138, "Pij eliksiry ~ Drink elixirs"),
            ]:
                result = state.install(Pair.select(*names), GIBBED, APPROVE)
                self.assertEqual((result.changed, result.key_matched, result.protected), (1, 1, 0))
                self.assertEqual(verify_text(result.data, target, index), expected)
                self.assertEqual(codec.inspect_table(result.data, Path(target).stem).keys["same_key"], index)
            self.assertEqual(set(state.restore(APPROVE)), {EN, PL})
        for name, data in original.items():
            self.assertEqual((self.cooked / name).read_bytes(), data)

    def test_literal_controls_and_unicode_remain_exact_in_gibbed_and_native_paths(self):
        english = "e\u0301 & <tag> \U00020000 \u200f\u2067 \t literal &#13;"
        japanese = "日本語\x0b한국어\r\nnext"
        self.put(EN, {1: english, 2: "é & <tag> \U00020000 \u200e"})
        self.put(JP, {1: japanese, 2: "繁體中文"})
        state = GameState(self.root)
        with state.locked():
            result = state.install(Pair.select("English", "Japanese"), GIBBED, APPROVE)
            self.assertEqual((result.native_encoded, result.protected), (1, 0))
            self.assertEqual(verify_text(result.data, JP, 1), english + codec.SEPARATOR + japanese)
            self.assertEqual(verify_text(result.data, JP, 2), "é & <tag> \U00020000 \u200e ~ 繁體中文")

    def test_duplicate_binary_id_rejected_before_journal(self):
        original = self.put(EN, {1: "English", 2: "Second"})
        donor = self.put(PL, {1: "Polski", 2: "Drugi"})
        parsed = codec.inspect_table(original, "en0")
        damaged = original[:-len(parsed.records[2].encoded)]
        damaged += struct.pack("<I", 1 ^ parsed.mask) + parsed.records[2].encoded[4:]
        (self.cooked / EN).write_bytes(damaged)
        state = GameState(self.root)
        with state.locked():
            with self.assertRaisesRegex(codec.FormatError, "duplicate text ID"):
                state.install(PAIR, GIBBED, lambda _: self.fail("Unexpected approval"))
            self.assertFalse(state.journal.exists())
        self.assertEqual((self.cooked / EN).read_bytes(), damaged)
        self.assertEqual((self.cooked / PL).read_bytes(), donor)

    def test_invalid_utf16_is_preserved_while_other_native_records_merge(self):
        raw = self.put(TR, {1: "Türkçe", 2: "x"})
        base = codec.inspect_table(raw, "tr0")
        malformed = codec.transform_text(b"\x00\xd8", base.mask)
        first = base.records[1]
        check = codec.checksum(malformed) + first.checksum
        damaged = (base.header + struct.pack("<I", check ^ base.mask) + codec.signed_count(2)
                   + first.encoded + struct.pack("<I", 2 ^ base.mask) + b"\x01" + malformed)
        (self.cooked / TR).write_bytes(damaged)
        self.put(EN, {1: "English", 2: "Otherwise valid"})
        state = GameState(self.root)
        with state.locked():
            result = state.install(Pair.select("Turkish", "English"), GIBBED, APPROVE)
            self.assertEqual((result.changed, result.opaque_kept), (1, 1))
            self.assertEqual(verify_text(result.data, TR, 1), "Türkçe ~ English")
            actual = codec.inspect_table(result.data, "tr0").records[2]
            self.assertEqual(actual.encoded, codec.inspect_table(damaged, "tr0").records[2].encoded)
            self.assertEqual(state.restore(APPROVE), (TR,))
        self.assertEqual((self.cooked / TR).read_bytes(), damaged)

    def test_reads_active_p0_journal_and_restores_p0_bytes(self):
        p0 = os.environ.get("W2_P0_SOURCE")
        if not p0:
            self.skipTest("Set W2_P0_SOURCE to the extracted P0 TEST 1 Source directory.")
        originals = {EN: self.put(EN, {1: "English"}), PL: self.put(PL, {1: "Polski"})}
        script = """
from pathlib import Path
from w2_install import GameState, Pair
import sys
state = GameState(sys.argv[1])
with state.locked():
    state.install(Pair.select("English", "Polish"), Path(sys.argv[2]), lambda _: True)
"""
        proc = subprocess.run([sys.executable, "-c", script, str(self.root), str(GIBBED)],
                              cwd=p0, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        self.assertEqual(verify_text((self.cooked / EN).read_bytes(), EN, 1), "English ~ Polski")
        state = GameState(self.root)
        with state.locked():
            result = state.install(Pair.select("Polish", "English"), GIBBED, APPROVE)
            self.assertEqual(verify_text(result.data, PL, 1), "Polski ~ English")
            self.assertEqual(set(state.restore(APPROVE)), {EN, PL})
        for name, data in originals.items():
            self.assertEqual((self.cooked / name).read_bytes(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

