"""P3 consent, format boundaries and compatibility regression."""
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from smoke_p3 import build_fixture, snapshot
from w2_font_test import FontTransaction, PAIRS, PROFILES, run_probe
from w2_install import GameState, StateError

TOOLS = Path(__file__).resolve().parents[1] / "Gibbed RED Tools"


class P3Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-p3-unit-")
        self.addCleanup(temp.cleanup)
        strings = Path(os.environ["W2_TEST_COPY_SOURCE"])
        assets = Path(os.environ["W2_P3_ASSETS"])
        self.game, self.cooked, _ = build_fixture(Path(temp.name), strings, assets)

    def test_declining_new_sources_never_writes_game_or_registers(self):
        state = GameState(self.game)
        original = snapshot(self.cooked)
        with state.locked():
            with patch("w2_font_test.ensure_game_closed") as check:
                self.assertIsNone(run_probe(state, ("TR", "ZH"), TOOLS, apply=True, approve=lambda _: False))
                check.assert_not_called()
            self.assertEqual(state.load()["slots"], {})
            self.assertIsNone(FontTransaction(state).load())
        self.assertEqual(snapshot(self.cooked), original)

    def test_modified_source_after_confirmation_blocks_before_replacement(self):
        state = GameState(self.game)
        target = self.cooked / "tr0.w2strings"
        donor = self.cooked / "zh0.w2strings"
        original = target.read_bytes()
        modified = donor.read_bytes() + b"external change"
        def confirm(names):
            self.assertEqual(set(names), {target.name, donor.name})
            donor.write_bytes(modified)
            return True

        with state.locked(), patch("w2_font_test.ensure_game_closed"):
            with self.assertRaisesRegex(StateError, "changed during source verification"):
                run_probe(state, ("TR", "ZH"), TOOLS, apply=True, approve=confirm)
            self.assertIsNone(FontTransaction(state).load())
            self.assertEqual(state.load()["slots"], {})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(donor.read_bytes(), modified)

    def test_headless_worker_installs_p3_and_restores_without_pair(self):
        from Witcher2_Dual_Subtitles_Tool import App
        recorded = []
        def answer(event):
            recorded.append(event[:1])
            if event[0] == "ask":
                event[1][2].put(True)

        worker = SimpleNamespace(events=SimpleNamespace(put=answer))
        original = snapshot(self.cooked)
        with patch("w2_font_test.ensure_game_closed"):
            App.worker(worker, self.game, "install", ("JP", "TR"), TOOLS / "Gibbed.RED.Strings.exe")
            self.assertEqual(recorded, [("ask",), ("done",)])
            changed = set(snapshot(self.cooked).items()) - set(original.items())
            self.assertEqual({name for name, _ in changed}, {"jp0.w2strings", "globals/gui/fonts_jp.swf"})
            App.worker(worker, self.game, "restore", None, None)
        self.assertEqual(recorded, [("ask",), ("done",), ("ask",), ("done",)])
        self.assertEqual(snapshot(self.cooked), original)

    def test_registry_lists_all_ordered_pairs_and_leaves_unverified_kr_disabled(self):
        self.assertEqual(len(PROFILES), 12)
        self.assertEqual(len(PAIRS), len(PROFILES) * (len(PROFILES) - 1))
        self.assertNotIn("KR", PROFILES)
        self.assertTrue({("JP", "ZH"), ("ZH", "JP"), ("TR", "JP"), ("JP", "TR"),
                         ("TR", "ZH"), ("ZH", "TR")} <= PAIRS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
