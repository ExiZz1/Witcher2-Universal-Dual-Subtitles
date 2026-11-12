"""Run on Windows with Python 3.13 and the bundled Gibbed tools."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import w2_codec as codec
from w2_install import GameState, Pair, StateError, LEGACY_DIR, digest

GIBBED = ROOT / "Gibbed RED Tools" / "Gibbed.RED.Strings.exe"
EN, PL, JP, TR, ZH = (language + "0.w2strings" for language in ("en", "pl", "jp", "tr", "zh"))
PAIR = Pair.select("English", "Polish")
APPROVE = lambda _: True


def fixture(slot, texts, keys=None, version=201):
    key, mask = codec.LOCALE_KEYS[slot]
    names = {"shared_anchor": 1} if keys is None else keys
    header = struct.pack("<IH", version, key >> 16) + codec.signed_count(len(names))
    for name, index in names.items():
        value = name.encode("cp1252")
        header += bytes([0x80 | len(value)]) + value + struct.pack("<I", index ^ mask)
    header += struct.pack("<H", key & 0xFFFF)
    total, records = 0, bytearray()
    for index, text in texts.items():
        raw = codec.transform_text(text.encode("utf-16le"), mask)
        total += codec.checksum(raw)
        records += struct.pack("<I", index ^ mask) + codec.signed_count(len(raw) // 2) + raw
    return header + struct.pack("<I", (total & 0xFFFFFFFF) ^ mask) + codec.signed_count(len(texts)) + records


def verify_text(data, name, index):
    table = codec.inspect_table(data, Path(name).stem)
    record = table.records[index]
    return codec.transform_text(record.encrypted, table.mask).decode("utf-16le")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="w2-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cooked = self.root / "CookedPC"
        self.cooked.mkdir()
        self.original = {
            EN: fixture("en0", {1: "English", 2: "Same", 3: "Solo"}),
            PL: fixture("pl0", {3: "", 2: "Same", 1: "Polski"}),
            JP: fixture("jp0", {1: "日本語", 2: "Same", 3: ""}),
            TR: fixture("tr0", {1: "Türkçe", 2: "Same", 3: ""}),
            ZH: fixture("zh0", {1: "繁體中文", 2: "Same", 3: ""}),
        }
        for name, data in self.original.items():
            (self.cooked / name).write_bytes(data)

    def game_bytes(self):
        return {name: (self.cooked / name).read_bytes() for name in self.original}

    def install(self, primary="English", secondary="Polish", approve=APPROVE):
        pair = Pair.select(primary, secondary)
        state = GameState(self.root)
        with state.locked():
            return state.install(pair, GIBBED, approve)

    def restore(self):
        state = GameState(self.root)
        with state.locked(create=False) as locked:
            return state.restore(APPROVE) if locked else ()

    def slot_text(self, name, index=1):
        return verify_text((self.cooked / name).read_bytes(), name, index)

    def child(self, action, expected):
        completed = subprocess.run(
            [sys.executable, __file__, action, str(self.root)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)

    def test_install_repeat_reverse_switch_and_restore(self):
        asked = []
        self.assertEqual(self.install(approve=lambda new: not asked.append(set(new))).changed, 1)
        self.assertEqual(asked, [{EN, PL}])
        self.assertEqual(self.slot_text(EN), "English ~ Polski")
        first = self.game_bytes()
        self.install(approve=lambda _: self.fail("Verified source unexpectedly required approval."))
        self.assertEqual(self.game_bytes(), first)
        self.install("Polish", "English")
        self.assertEqual(self.slot_text(PL), "Polski ~ English")
        self.assertEqual(self.slot_text(EN), "English ~ Polski")
        self.install("Japanese", "English")
        self.assertEqual(self.slot_text(JP), "日本語 ~ English")
        self.install("English", "Japanese")
        self.assertEqual(self.slot_text(JP), "English ~ 日本語")
        state = GameState(self.root)
        with state.locked():
            receipt = state.state["last"]
            self.assertEqual(receipt["sources"], {EN: digest(self.original[EN]), JP: digest(self.original[JP])})
            self.assertEqual(receipt["files"][0]["original"], digest(self.original[JP]))
            self.assertEqual(receipt["files"][0]["after"], digest(self.game_bytes()[JP]))
            self.assertNotEqual(receipt["files"][0]["before"], receipt["files"][0]["after"])
        self.assertEqual(set(self.restore()), {EN, PL, JP})
        self.assertEqual(self.game_bytes(), self.original)
        self.assertEqual(self.restore(), ())

    def test_rejected_pair_does_not_undo_previous_install(self):
        self.install("English", "Japanese")
        before = self.game_bytes()
        for pair in [("Turkish", "Japanese"), ("Japanese", "Turkish"), ("Japanese", "Chinese")]:
            with self.subTest(pair=pair), self.assertRaises(StateError):
                self.install(*pair)
        self.assertEqual(self.game_bytes(), before)

    def test_declined_provenance_leaves_files_and_journal_untouched(self):
        self.assertIsNone(self.install(approve=lambda _: False))
        self.assertEqual(self.game_bytes(), self.original)
        self.assertFalse(GameState(self.root).journal.exists())

    def test_preparation_failure_leaves_active_result(self):
        self.install()
        before = self.game_bytes()
        journal = GameState(self.root).journal.read_bytes()
        with patch("w2_install.decode_source", side_effect=codec.FormatError("injected decode failure")):
            with self.assertRaisesRegex(codec.FormatError, "decode failure"):
                self.install()
        actual_run = codec.run_gibbed
        def fail_only_on_encode(exe, option, source, target):
            if option == "-e":
                raise codec.FormatError("injected encode failure")
            return actual_run(exe, option, source, target)
        with patch("w2_codec.run_gibbed", side_effect=fail_only_on_encode):
            with self.assertRaisesRegex(codec.FormatError, "encode failure"):
                self.install()
        self.assertEqual(self.game_bytes(), before)
        self.assertEqual(GameState(self.root).journal.read_bytes(), journal)

    def test_failure_at_atomic_rename_does_not_truncate_target(self):
        actual = os.replace
        def fail(source, target):
            if target == self.cooked / EN:
                raise OSError("injected final rename failure")
            return actual(source, target)
        with patch("w2_install.os.replace", side_effect=fail):
            with self.assertRaisesRegex(StateError, "rolled back"):
                self.install()
        self.assertEqual(self.game_bytes(), self.original)
        with GameState(self.root).locked() as locked:
            self.assertTrue(locked)
        self.assertIn('"pending": null', GameState(self.root).journal.read_text())

    def test_failure_after_replace_rolls_back_to_previous_install(self):
        self.install("English", "Japanese")
        before = self.game_bytes()
        replace = GameState.replace
        def fail_after_write(state, name, data, expected):
            replace(state, name, data, expected)
            if expected == digest(before[JP]):
                raise OSError("injected post-replace failure")
        with patch.object(GameState, "replace", fail_after_write):
            with self.assertRaisesRegex(StateError, "rolled back"):
                self.install("Japanese", "English")
        self.assertEqual(self.game_bytes(), before)

    def test_interrupted_install_recovers_on_reopen(self):
        self.child("--crash-install", 73)
        self.assertEqual(self.slot_text(EN), "English ~ Polski")
        state = GameState(self.root)
        with state.locked():
            self.assertIsNotNone(state.state["pending"])
            self.assertTrue(state.recover())
            self.assertFalse(state.recover())
        self.assertEqual(self.game_bytes(), self.original)

    def test_interrupted_multislot_restore_rolls_back_and_can_retry(self):
        self.install()
        self.install("Polish", "English")
        before = self.game_bytes()
        self.child("--crash-restore", 73)
        self.assertNotEqual(self.game_bytes(), before)
        state = GameState(self.root)
        with state.locked():
            self.assertTrue(state.recover())
        self.assertEqual(self.game_bytes(), before)
        self.assertEqual(set(self.restore()), {EN, PL})
        self.assertEqual(self.game_bytes(), self.original)

    def test_recovery_preserves_external_conflict(self):
        self.child("--crash-install", 73)
        (self.cooked / EN).write_bytes(b"external")
        before = self.game_bytes()
        state = GameState(self.root)
        with state.locked():
            with self.assertRaisesRegex(StateError, "external change"):
                state.recover()
            self.assertIsNotNone(state.state["pending"])
        self.assertEqual(self.game_bytes(), before)

    def test_second_process_cannot_lock_same_installation(self):
        state = GameState(self.root)
        with state.locked():
            self.child("--lock", 74)
        self.child("--lock", 0)

    def test_unknown_external_live_file_is_preserved(self):
        self.install()
        self.install("Polish", "English")
        (self.cooked / EN).write_bytes(fixture("en0", {1: "Third party", 2: "Same"}))
        before = self.game_bytes()
        with self.assertRaisesRegex(StateError, "external"):
            self.install("Polish", "English")
        with self.assertRaisesRegex(StateError, "outside"):
            self.restore()
        self.assertEqual(self.game_bytes(), before)

    def test_external_change_during_confirmation_blocks_commit(self):
        new = fixture("pl0", {1: "Edited during approval", 2: "Same"})
        def change_donor(_):
            (self.cooked / PL).write_bytes(new)
            return True
        with self.assertRaisesRegex(StateError, "changed outside"):
            self.install(approve=change_donor)
        self.assertEqual(self.game_bytes(), {**self.original, PL: new})
        self.assertFalse(GameState(self.root).journal.exists())

    def test_external_exact_restore_is_reconciled_without_replacement(self):
        self.install()
        (self.cooked / EN).write_bytes(self.original[EN])
        with patch.object(GameState, "replace", side_effect=AssertionError("unexpected write")):
            self.assertEqual(self.restore(), (EN,))
        self.assertEqual(self.game_bytes(), self.original)

    def test_legacy_stale_or_corrupt_and_matching_backups(self):
        legacy = self.cooked / LEGACY_DIR
        legacy.mkdir()
        backup = legacy / (EN + ".original")
        for data in (b"truncated", fixture("en0", {1: "Older revision", 2: "Same"})):
            backup.write_bytes(data)
            with self.assertRaisesRegex(StateError, "Legacy backup"):
                self.install()
            self.assertEqual(self.game_bytes(), self.original)
            self.assertEqual(backup.read_bytes(), data)
        backup.write_bytes(self.original[EN])
        asked = []
        self.assertIsNone(self.install(approve=lambda new: asked.append(new)))
        self.assertEqual(set(asked[0]), {EN, PL})
        self.install()
        self.assertEqual(backup.read_bytes(), self.original[EN])

    def test_corrupt_original_snapshot_blocks_install_and_restore(self):
        self.install()
        state = GameState(self.root)
        (state.objects / digest(self.original[EN])).write_bytes(b"corrupt")
        before = self.game_bytes()
        for command in (self.install, self.restore):
            with self.assertRaisesRegex(StateError, "SHA-256"):
                command()
            self.assertEqual(self.game_bytes(), before)

    def test_new_backup_is_verified_before_first_write(self):
        replace = os.replace
        snapshot = GameState(self.root).objects / digest(self.original[EN])

        def corrupt_backup(source, target):
            replace(source, target)
            if target == snapshot:
                target.write_bytes(b"damaged immediately after write")

        with patch("w2_install.os.replace", side_effect=corrupt_backup):
            with self.assertRaisesRegex(StateError, "SHA-256"):
                self.install()
        self.assertEqual(self.game_bytes(), self.original)
        self.assertFalse(GameState(self.root).journal.exists())

    def test_tk_worker_entry_recovers_and_restores_without_language_selection(self):
        from Witcher2_Dual_Subtitles_Tool import App

        self.child("--crash-install", 73)
        events = []

        def accept(event):
            label, payload = event
            events.append(label)
            if label == "ask":
                payload[2].put(True)

        headless = SimpleNamespace(events=SimpleNamespace(put=accept))
        App.worker(headless, self.root, "recover", None, None)
        self.assertEqual(events, ["done"])
        self.assertEqual(self.game_bytes(), self.original)
        # Build a legacy P0 installation, then restore it through the P3 worker.
        self.install()
        self.assertEqual(self.slot_text(EN), "English ~ Polski")
        App.worker(headless, self.root, "restore", None, None)
        self.assertEqual(events, ["done", "ask", "done"])
        self.assertEqual(self.game_bytes(), self.original)

    def test_untracked_legacy_output_is_not_adopted(self):
        dual = fixture("en0", {1: "English ~ Polski", 2: "Same"})
        (self.cooked / EN).write_bytes(dual)
        with self.assertRaisesRegex(StateError, "contains the dual separator"):
            self.install(approve=lambda _: self.fail("Legacy dual output reached approval."))
        self.assertEqual(self.game_bytes(), {**self.original, EN: dual})

    def test_legitimate_separator_can_be_verified_on_new_revision(self):
        updated = fixture("en0", {1: "Genuine ~ punctuation", 2: "Same"})
        (self.cooked / EN).write_bytes(updated)
        checked = []
        self.install(approve=lambda new: not checked.append(new))
        self.assertIn("dual separator", checked[0][EN])
        self.assertEqual(self.slot_text(EN), "Genuine ~ punctuation ~ Polski")
        self.restore()
        self.assertEqual((self.cooked / EN).read_bytes(), updated)

    def test_compatible_revision_rebased_by_structure(self):
        self.install()
        self.restore()
        for name, text in ((EN, "English revised"), (PL, "Polski nowy")):
            revised = fixture(Path(name).stem, {90210: text, 2: "Same", 1: "Same anchor"},
                              {"shared_anchor": 1, "added_entry": 90210})
            (self.cooked / name).write_bytes(revised)
        revised = self.game_bytes()
        seen = []
        self.install(approve=lambda new: not seen.append(new))
        self.assertEqual(set(seen[0]), {EN, PL})
        self.assertEqual(self.slot_text(EN, 90210), "English revised ~ Polski nowy")
        self.restore()
        self.assertEqual(self.game_bytes(), revised)

    def test_malformed_revision_wrong_key_checksum_and_mappings(self):
        damaged = bytearray(self.original[EN])
        damaged[-1] ^= 1
        cases = [
            (EN, fixture("en0", {1: "New version"}, version=202)),
            (EN, self.original[PL]), (EN, self.original[EN][:-1]),
            (EN, bytes(damaged)), (PL, fixture("pl0", {1: "P"}, {"unrelated": 1})),
        ]
        for name, bad in cases:
            with self.subTest(name=name, bad=bad[:8]):
                (self.cooked / name).write_bytes(bad)
                before = self.game_bytes()
                with self.assertRaises(codec.FormatError):
                    self.install(approve=lambda _: self.fail("Invalid data reached approval."))
                self.assertEqual(self.game_bytes(), before)
                (self.cooked / name).write_bytes(self.original[name])

    def test_ambiguous_named_ids_remain_unchanged(self):
        primary = fixture("en0", {1: "EN", 2: "Conflict", 3: "Raw EN"},
                          {"shared_anchor": 1, "same_alias": 1, "name_changed_id": 2})
        secondary = fixture("pl0", {1: "PL", 2: "Other", 3: "Conflict"},
                            {"shared_anchor": 1, "same_alias": 1, "name_changed_id": 3})
        (self.cooked / EN).write_bytes(primary)
        (self.cooked / PL).write_bytes(secondary)
        result = self.install()
        self.assertEqual(result.protected, 2)
        self.assertEqual(self.slot_text(EN, 1), "EN ~ PL")
        self.assertEqual(self.slot_text(EN, 2), "Conflict")
        self.assertEqual(self.slot_text(EN, 3), "Raw EN")

    def test_unicode_xml_and_cjk_binary_roundtrip(self):
        english = "Ąęł & <tag> literal &#13; e\u0301 \U00020000\r\nnext"
        japanese = "漢字かなカナ 繁體 한국어 & <日本>"
        originals = {
            EN: fixture("en0", {1: english, 2: "A\x0bB", 3: "ordinary \U00020000 & <tag>"}),
            JP: fixture("jp0", {1: japanese, 2: "安全", 3: "日本語"}),
        }
        for name, data in originals.items():
            (self.cooked / name).write_bytes(data)
        result = self.install("Japanese", "English")
        self.assertEqual(result.protected, 0)
        self.assertEqual(self.slot_text(JP), japanese + codec.SEPARATOR + english)
        self.assertEqual(self.slot_text(JP, 2), "安全 ~ A\x0bB")
        self.assertEqual(self.slot_text(JP, 3), "日本語 ~ ordinary \U00020000 & <tag>")
        result = self.install("English", "Japanese")
        self.assertEqual(self.slot_text(JP), english + codec.SEPARATOR + japanese)
        self.assertEqual(self.slot_text(JP, 2), "A\x0bB ~ 安全")
        self.restore()
        self.assertEqual((self.cooked / JP).read_bytes(), originals[JP])

    def test_turkish_adapter_both_directions(self):
        self.install("Turkish", "English")
        self.assertEqual(self.slot_text(TR), "Türkçe ~ English")
        self.install("English", "Turkish")
        self.assertEqual(self.slot_text(EN), "English ~ Türkçe")
        self.assertEqual(set(self.restore()), {EN, TR})
        self.assertEqual(self.game_bytes(), self.original)


class GameCopyTests(unittest.TestCase):
    def test_all_twelve_real_sources_and_requested_pairs(self):
        directory = os.environ.get("W2_TEST_COPY_SOURCE")
        if not directory:
            self.skipTest("Set W2_TEST_COPY_SOURCE to a folder of clean copied v201 files.")
        originals = {path.name: path.read_bytes() for path in Path(directory).glob("*.w2strings")}
        self.assertEqual(set(originals), {stem + ".w2strings" for stem in codec.LOCALE_KEYS})
        with tempfile.TemporaryDirectory(prefix="w2-real-copies-") as tmp:
            root = Path(tmp)
            cooked = root / "CookedPC"
            cooked.mkdir()
            for name, data in originals.items():
                (cooked / name).write_bytes(data)
                with tempfile.TemporaryDirectory(dir=root) as staging:
                    decoded = codec.decode_source(data, Path(name).stem, GIBBED, Path(staging))
                    self.assertFalse(decoded.marked_ids, name)
                    self.assertGreater(len(decoded.texts), 1000, name)
            state = GameState(root)
            with state.locked():
                results = {}
                for a, b in [("English", "Polish"), ("English", "Polish"), ("Polish", "English"),
                             ("Japanese", "English"), ("English", "Japanese"), ("English", "Chinese"),
                             ("Turkish", "English"), ("English", "Turkish")]:
                    pair = Pair.select(a, b)
                    result = state.install(pair, GIBBED, APPROVE)
                    self.assertGreater(result.changed, 1000)
                    self.assertEqual((cooked / pair.target).read_bytes(), result.data)
                    previous = results.setdefault((a, b), digest(result.data))
                    self.assertEqual(previous, digest(result.data))
                    if pair.target != TR:
                        with tempfile.TemporaryDirectory(dir=root) as staging:
                            output = Path(staging) / "decoded.xml"
                            codec.run_gibbed(GIBBED, "-d", cooked / pair.target, output)
                            parsed = codec.parse_gibbed_xml(
                                output, codec.inspect_table(result.data, Path(pair.target).stem),
                            )
                            self.assertEqual(len(parsed), result.total)
                    print(f"  game copy {a} -> {b}: {result.changed:,} merged, {result.protected:,} protected",
                          flush=True)
                self.assertEqual(set(state.restore(APPROVE)), {EN, PL, JP, ZH, TR})
            for name, data in originals.items():
                self.assertEqual((cooked / name).read_bytes(), data, name)


def child(action, root):
    state = GameState(root)
    try:
        with state.locked():
            if action == "--lock":
                return 0
            replace = state.replace
            def quit_after_replace(name, data, expected):
                replace(name, data, expected)
                os._exit(73)
            state.replace = quit_after_replace
            if action == "--crash-install":
                state.install(PAIR, GIBBED, APPROVE)
            else:
                state.restore(APPROVE)
    except StateError as error:
        if action == "--lock" and "Another" in str(error):
            return 74
        raise
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--lock", "--crash-install", "--crash-restore"):
        sys.exit(child(sys.argv[1], sys.argv[2]))
    unittest.main(verbosity=2)

