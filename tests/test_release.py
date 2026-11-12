"""Headless release checks for layout and compatible source refresh."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from smoke_p3 import ROOT, build_fixture, snapshot
from w2_codec import assemble_changes, decode_source
from w2_font_test import FontTransaction, run_probe
from w2_install import GameState, StateError, digest
from Witcher2_Dual_Subtitles_Tool import (
    APP_VERSION, App, PRIMARY_HINT, find_gibbed, fit_window_dpi, is_game_folder, window_dimensions, window_layout,
)
from w2_windows_ui import (
    HTCAPTION, SC_MAXIMIZE, SC_SIZE, SWP_NOSIZE, WM_NCLBUTTONDBLCLK, WM_SYSCOMMAND,
    WS_MAXIMIZEBOX, WS_THICKFRAME, WindowPos, blocks_resize_message, centered_origin,
    constrain_window_position, fixed_window_style,
)

TOOLS = ROOT / "Gibbed RED Tools"


class ReleaseCandidateTests(unittest.TestCase):
    def test_compact_layout_at_supported_dpi(self):
        for dpi, expected in ((96, (760, 480)), (120, (950, 600)), (144, (1140, 720))):
            with self.subTest(dpi=dpi):
                def px(value):
                    return round(value * dpi / 96)

                self.assertEqual(window_dimensions(dpi), expected)
                top, footer, line = (px(570), px(265)), (px(320), px(25)), px(18)
                log_padding = 2 * (px(7) + 1)
                minimum_log = 5 * line + log_padding
                maximum_log = 14 * line + log_padding
                compact_width, compact_log, minimum = window_layout(dpi, *expected, top, footer, line)
                self.assertEqual(minimum, (px(660), top[1] + footer[1] + 2 * px(18) + minimum_log))
                minimum_width, min_log, _ = window_layout(dpi, *minimum, top, footer, line)
                self.assertEqual(minimum_width, minimum[0] - 2 * px(24))
                self.assertEqual(min_log, minimum_log)
                self.assertEqual(compact_width, expected[0] - 2 * px(24))
                self.assertGreater(compact_log, minimum_log)
                self.assertEqual(top[1] + footer[1] + compact_log + 2 * px(18), expected[1])
                self.assertLess(compact_log, maximum_log)

    def test_minimum_accounts_for_measured_controls_and_font(self):
        panel, log, minimum = window_layout(96, 600, 350, (670, 320), (240, 30), 22)
        self.assertEqual(minimum, (718, 512))
        self.assertEqual(log, 126)
        self.assertEqual(panel, 552)
        self.assertEqual(window_layout(96, *minimum, (670, 320), (240, 30), 22), (670, 126, minimum))

    def test_small_work_area_fits_all_controls_and_centering_handles_negative_origins(self):
        for dpi in (96, 120, 144):
            for work_width, work_height in ((1920, 1040), (1280, 680), (1024, 560), (800, 480)):
                with self.subTest(dpi=dpi, work=(work_width, work_height)):
                    fitted = fit_window_dpi(dpi, work_width, work_height)
                    width, height = window_dimensions(fitted)
                    self.assertLessEqual(fitted, dpi)
                    self.assertLessEqual(width + round(24 * dpi / 96), work_width)
                    self.assertLessEqual(height + round(52 * dpi / 96), work_height)
                    px = lambda value: round(value * fitted / 96)
                    top, footer, line = (px(570), px(265)), (px(320), px(25)), px(18)
                    panel, log, minimum = window_layout(fitted, width, height, top, footer, line)
                    self.assertGreaterEqual(panel, top[0])
                    self.assertGreaterEqual(log - 2 * (px(7) + 1), 5 * line)
                    self.assertLessEqual(minimum[1], height)
        self.assertEqual(centered_origin((-1920, -30, 0, 1010), 760, 480), (-1340, 250))

    def test_native_maximize_and_size_commands_blocked_but_window_controls_preserved(self):
        preserve = 0x00020000 | 0x00080000 | 0x00C00000
        self.assertEqual(fixed_window_style(preserve | WS_THICKFRAME | WS_MAXIMIZEBOX), preserve)
        for command in (SC_SIZE, SC_MAXIMIZE, SC_MAXIMIZE | 2):
            self.assertTrue(blocks_resize_message(WM_SYSCOMMAND, command))
        for command in (0xF010, 0xF020, 0xF060, 0xF120):
            self.assertFalse(blocks_resize_message(WM_SYSCOMMAND, command))
        self.assertTrue(blocks_resize_message(WM_NCLBUTTONDBLCLK, HTCAPTION))
        self.assertFalse(blocks_resize_message(WM_NCLBUTTONDBLCLK, 3))

    def test_direct_snap_size_is_constrained_without_blocking_move_or_minimize(self):
        pos = WindowPos(x=-900, y=30, width=950, height=990)
        constrain_window_position(pos, 760, 480, minimized=False)
        self.assertEqual((pos.x, pos.y, pos.width, pos.height), (-900, 30, 760, 480))
        for flags, minimized in ((SWP_NOSIZE, False), (0, True)):
            pos = WindowPos(width=20, height=20, flags=flags)
            constrain_window_position(pos, 760, 480, minimized=minimized)
            self.assertEqual((pos.width, pos.height), (20, 20))

    def test_relayout_centers_panel_at_fixed_compact_size(self):
        app = SimpleNamespace(
            dpi=96, log_line_height=18, layout_job=None, client_size=(760, 480),
            top=SimpleNamespace(winfo_reqwidth=lambda: 570, winfo_reqheight=lambda: 265),
            footer=SimpleNamespace(winfo_reqwidth=lambda: 320, winfo_reqheight=lambda: 25),
            after_idle=Mock(return_value="resize"),
            logbox=Mock(), panel=Mock(),
        )
        app.measure_layout = lambda width, height: App.measure_layout(app, width, height)
        app.update_layout = lambda: App.update_layout(app)
        App.schedule_layout(app)
        App.schedule_layout(app)
        app.after_idle.assert_called_once_with(app.update_layout)
        app.update_layout()
        self.assertIsNone(app.layout_job)
        app.logbox.configure.assert_called_once_with(height=154)
        app.panel.place.assert_called_once_with(relx=0.5, rely=0.5, anchor="center", width=712)

    def test_release_label_and_primary_hint(self):
        self.assertEqual(APP_VERSION, "v3.0")
        self.assertEqual(PRIMARY_HINT, "Set the game's Text Language to your selected Primary language.")
        self.assertFalse(hasattr(App, "show_guide"))

    def test_log_restores_readonly_state(self):
        events = []
        log = SimpleNamespace(configure=lambda **opts: events.append(opts), insert=lambda *args: events.append(args),
                              delete=lambda *args: events.append(args), see=lambda *args: events.append(args))
        app = SimpleNamespace(log=log)
        App.write_log(app, "sample")
        App.clear_log(app)
        self.assertEqual(events, [{"state": "normal"}, ("end", "sample\n"), {"state": "disabled"}, ("end",),
                                  {"state": "normal"}, ("1.0", "end"), {"state": "disabled"}])

    def test_detection_and_runtime_tool_path(self):
        self.assertEqual(find_gibbed(ROOT), TOOLS / "Gibbed.RED.Strings.exe")
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            (root / "CookedPC").mkdir()
            (root / "CookedPC/pl0.w2strings").touch()
            self.assertFalse(is_game_folder(root))
            (root / "CookedPC/pack0.dzip").touch()
            self.assertTrue(is_game_folder(root))
            (root / "Gibbed.RED.Strings.exe").touch()
            self.assertIsNone(find_gibbed(root))

    def test_structurally_valid_update_revalidates_without_fixed_sha(self):
        strings = Path(os.environ["W2_TEST_COPY_SOURCE"])
        assets = Path(os.environ["W2_P3_ASSETS"])
        with tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/Temp", prefix="w2-rc-revision-") as name:
            game, cooked, _ = build_fixture(Path(name), strings, assets)
            state = GameState(game)
            with state.locked(), patch("w2_font_test.ensure_game_closed"):
                tx = FontTransaction(state)
                self.assertTrue(run_probe(state, ("EN", "PL"), TOOLS, apply=True, approve=lambda _: True)["applied"])
                self.assertTrue(tx.restore())
                target = cooked / "en0.w2strings"
                original_hash = digest(target.read_bytes())
                work = Path(name) / "revision"
                work.mkdir()
                exe = TOOLS / "Gibbed.RED.Strings.exe"
                original = decode_source(target.read_bytes(), "en0", exe, work)
                idx = next(i for i, t in original.texts.items() if t.strip() and len(t) < 25)
                updated = assemble_changes(original.table, {idx: original.texts[idx] + " Revised"}, "en0", exe, work)[0]
                target.write_bytes(updated)
                expected = snapshot(cooked)
                with self.assertRaisesRegex(StateError, "recorded original"):
                    run_probe(state, ("EN", "PL"), TOOLS, apply=True)
                self.assertIsNone(run_probe(state, ("EN", "PL"), TOOLS, apply=True, approve=lambda _: False))
                self.assertEqual(snapshot(cooked), expected)
                self.assertEqual(state.state["slots"][target.name]["original"], original_hash)
                observed = []
                report = run_probe(state, ("EN", "PL"), TOOLS, apply=True,
                                   approve=lambda reasons: not observed.append(reasons))
                self.assertTrue(report["applied"])
                self.assertEqual(observed, [{target.name: "changed since last verification"}])
                self.assertEqual(state.state["slots"][target.name]["original"], digest(updated))
                self.assertTrue(tx.restore())
                self.assertEqual(snapshot(cooked), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
