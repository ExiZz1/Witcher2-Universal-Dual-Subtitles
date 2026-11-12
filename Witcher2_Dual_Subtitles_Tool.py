# -*- coding: utf-8 -*-
"""The Witcher 2 - Universal Dual Subtitles by ExiZz."""

import os
import queue
import re
import sys
import threading
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont
from pathlib import Path
from w2_install import GameState, LANGS, STATE_DIR, StateError
from w2_font_test import P3_LANGUAGES

APP_TITLE = "The Witcher 2 — Universal Dual Subtitles"
APP_VERSION = "v3.0"
CREATOR_NICK = "ExiZz"
TOOLS_DIR = Path(__file__).resolve().parent / "Gibbed RED Tools"
PRIMARY_HINT = "Set the game's Text Language to your selected Primary language."
BACKGROUND, SURFACE, FIELD = "#181619", "#231e23", "#171419"
FOREGROUND, MUTED, BORDER = "#f4eeee", "#c6b9c2", "#51434d"
ACCENT, HOVER = "#7d2d46", "#9a3b58"


def window_dimensions(dpi):
    return tuple(round(value * dpi / 96) for value in (760, 480))


def fit_window_dpi(dpi, work_width, work_height):
    frame_width, frame_height = (round(value * dpi / 96) for value in (24, 52))
    return max(1, min(dpi, (work_width - frame_width) * 96 // 760,
                      (work_height - frame_height) * 96 // 480))


def window_layout(dpi, width, height, top_size, footer_size, line_height):
    horizontal, vertical, base_width, max_panel, log_inset = (
        round(value * dpi / 96) for value in (24, 18, 660, 900, 7)
    )
    panel_width = min(max_panel, max(1, width - 2 * horizontal))
    fixed_height = top_size[1] + footer_size[1] + 2 * vertical
    log_padding = 2 * (log_inset + 1)
    minimum_log = 5 * line_height + log_padding
    maximum_log = 14 * line_height + log_padding
    log_height = max(minimum_log, min(maximum_log, height - fixed_height))
    minimum_width = max(base_width, max(top_size[0], footer_size[0]) + 2 * horizontal)
    return panel_width, log_height, (minimum_width, fixed_height + minimum_log)


def is_game_folder(folder):
    cooked = Path(folder) / "CookedPC"
    return (cooked / "pack0.dzip").is_file() and any((cooked / name).is_file() for name in LANGS.values())


def find_gog_folders():
    import winreg

    key = r"SOFTWARE\GOG.com\Games"
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view) as games:
                for i in range(winreg.QueryInfoKey(games)[0]):
                    try:
                        with winreg.OpenKey(games, winreg.EnumKey(games, i)) as game:
                            name, _ = winreg.QueryValueEx(game, "gameName")
                            if isinstance(name, str) and name.lower().startswith("the witcher 2"):
                                path, _ = winreg.QueryValueEx(game, "path")
                                if isinstance(path, str) and path:
                                    yield Path(path)
                    except OSError:
                        continue
        except OSError:
            continue


def parse_steam_library_paths():
    """Return Steam library roots without requiring Steam to be running."""
    paths = []
    try:
        import winreg
        keys = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
        ]
        for hive, subkey, value in keys:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    p, _ = winreg.QueryValueEx(k, value)
                    if p:
                        paths.append(Path(p))
            except OSError:
                pass
    except Exception:
        pass

    roots = list(dict.fromkeys(str(p) for p in paths))
    libraries = []
    for steam in roots:
        sp = Path(steam)
        libraries.append(sp)
        vdf = sp / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                txt = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    libraries.append(Path(m.group(1).replace("\\\\", "\\")))
            except Exception:
                pass
    return list(dict.fromkeys(libraries))


def find_game():
    candidates = []
    for lib in parse_steam_library_paths():
        candidates += [
            lib / "steamapps" / "common" / "The Witcher 2",
            lib / "steamapps" / "common" / "the witcher 2",
            lib / "steamapps" / "common" / "The Witcher 2 Assassins of Kings Enhanced Edition",
        ]
    # Also check common manually-used locations and all fixed drives cheaply.
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidates += [
            Path(f"{drive}:/Steam/steamapps/common/The Witcher 2"),
            Path(f"{drive}:/SteamLibrary/steamapps/common/The Witcher 2"),
            Path(f"{drive}:/SteamLibrary/steamapps/common/the witcher 2"),
            Path(f"{drive}:/Games/Steam/steamapps/common/The Witcher 2"),
        ]
    return next((folder for folder in (*candidates, *find_gog_folders()) if is_game_folder(folder)), None)


def find_gibbed(tool_dir):
    folder = Path(tool_dir) / "Gibbed RED Tools"
    names = ("Gibbed.RED.Strings.exe", "Gibbed.RED.Unpack.exe",
             "Gibbed.RED.FileFormats.dll", "Gibbed.IO.dll", "NDesk.Options.dll")
    return folder / names[0] if all((folder / name).is_file() for name in names) else None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        from w2_windows_ui import lock_and_center_window, monitor_work_area

        self.withdraw()
        dpi = round(self.winfo_fpixels("1i"))
        work = monitor_work_area()
        fitted_dpi = fit_window_dpi(dpi, work[2] - work[0], work[3] - work[1])
        if fitted_dpi != dpi:
            self.tk.call("tk", "scaling", fitted_dpi / 72)
        dpi = fitted_dpi
        self.px = lambda value: round(value * dpi / 96)
        self.dpi = dpi
        self.title(APP_TITLE + " | " + APP_VERSION)
        self.resizable(False, False)
        self.configure(background=BACKGROUND)
        self.configure_styles()

        self.game = tk.StringVar()
        self.gibbed = tk.StringVar()
        self.primary_lang = tk.StringVar(value="English")
        self.secondary_lang = tk.StringVar(value="Polish")
        self.status = tk.StringVar(value="Detecting game...")
        self.mode_hint = tk.StringVar(value=PRIMARY_HINT)

        self.panel = ttk.Frame(self)
        self.panel.columnconfigure(0, weight=1)
        shell = self.top = ttk.Frame(self.panel)
        shell.grid(row=0, column=0, sticky="ew")
        shell.columnconfigure(0, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.emblem = ttk.Label(header)
        self.emblem.grid(row=0, column=0, rowspan=2, padx=(0, self.px(10)), sticky="w")
        self.emblem.grid_remove()
        ttk.Label(header, text="The Witcher 2", style="Heading.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="Universal Dual Subtitles", style="Subtitle.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Label(header, text=APP_VERSION, style="Muted.TLabel").grid(row=0, column=2, sticky="e")
        ttk.Label(header, text="Created by " + CREATOR_NICK, style="Muted.TLabel").grid(row=1, column=2, sticky="e")

        controls = ttk.Frame(shell, style="Surface.TFrame", padding=self.px(14))
        controls.grid(row=1, column=0, sticky="ew", pady=(self.px(14), self.px(9)))
        controls.columnconfigure(0, weight=1, uniform="languages")
        controls.columnconfigure(1, weight=1, uniform="languages")
        ttk.Label(controls, text="Game folder", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        path_row = ttk.Frame(controls, style="Surface.TFrame")
        path_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.px(4), self.px(10)))
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self.game, state="readonly").grid(row=0, column=0, sticky="ew")
        self.browse_btn = ttk.Button(path_row, text="Browse…", command=self.browse_game)
        self.browse_btn.grid(row=0, column=1, padx=self.px(7))
        self.rescan_btn = ttk.Button(path_row, text="Rescan", command=self.detect)
        self.rescan_btn.grid(row=0, column=2)
        ttk.Label(controls, text="Primary language", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(controls, text="Secondary language", style="Field.TLabel").grid(
            row=2, column=1, sticky="w", padx=(self.px(10), 0))
        self.primary_combo = ttk.Combobox(controls, textvariable=self.primary_lang, values=list(LANGS),
                                          state="readonly", width=15)
        self.secondary_combo = ttk.Combobox(controls, textvariable=self.secondary_lang, values=list(LANGS),
                                            state="readonly", width=15)
        self.primary_combo.grid(row=3, column=0, sticky="ew", pady=(self.px(4), 0))
        self.secondary_combo.grid(row=3, column=1, sticky="ew", padx=(self.px(10), 0), pady=(self.px(4), 0))
        for combo in (self.primary_combo, self.secondary_combo):
            combo.bind("<<ComboboxSelected>>", self.update_mode_hint)

        ttk.Label(shell, textvariable=self.mode_hint, style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        actions = ttk.Frame(shell)
        actions.grid(row=3, column=0, sticky="ew", pady=(self.px(8), self.px(12)))
        self.install_btn = ttk.Button(actions, text="Install", command=self.start_install, style="Accent.TButton")
        self.restore_btn = ttk.Button(actions, text="Restore", command=self.restore)
        self.install_btn.pack(side="left")
        self.restore_btn.pack(side="left", padx=self.px(8))
        ttk.Label(shell, text="Operation log").grid(row=4, column=0, sticky="w", pady=(0, self.px(4)))
        logbox = self.logbox = ttk.Frame(self.panel, style="Surface.TFrame", padding=1)
        logbox.grid(row=1, column=0, sticky="ew")
        logbox.grid_propagate(False)
        logbox.rowconfigure(0, weight=1)
        logbox.columnconfigure(0, weight=1)
        self.log = tk.Text(logbox, wrap="word", height=5, width=24, relief="flat", borderwidth=0,
                           highlightthickness=0, padx=self.px(10), pady=self.px(7), state="disabled",
                           background=FIELD, foreground=FOREGROUND, selectbackground=ACCENT,
                           selectforeground=FOREGROUND, font="TkDefaultFont")
        scrollbar = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.footer = ttk.Frame(self.panel, padding=(0, self.px(8), 0, 0))
        self.footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(self.footer, textvariable=self.status, style="Muted.TLabel").pack(anchor="w")
        self.log_line_height = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        self.layout_job = None
        self.update_idletasks()
        width, height = window_dimensions(dpi)
        _, _, minimum = self.measure_layout(width, height)
        self.client_size = max(width, minimum[0]), max(height, minimum[1])
        width, height = self.client_size
        self.geometry(f"{width}x{height}")
        self.minsize(width, height)
        self.maxsize(width, height)
        self.update_layout()

        self.busy = False
        self.events = queue.Queue()
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.deiconify()
        self.update_idletasks()
        lock_and_center_window(self, work)
        self.after(100, self.detect)

    def schedule_layout(self):
        if self.layout_job is None:
            self.layout_job = self.after_idle(self.update_layout)

    def measure_layout(self, width, height):
        top_size = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        footer_size = self.footer.winfo_reqwidth(), self.footer.winfo_reqheight()
        return window_layout(self.dpi, width, height, top_size, footer_size, self.log_line_height)

    def update_layout(self):
        self.layout_job = None
        panel_width, log_height, _ = self.measure_layout(*self.client_size)
        self.logbox.configure(height=log_height)
        self.panel.place(relx=0.5, rely=0.5, anchor="center", width=panel_width)

    def configure_styles(self):
        families = {name.casefold(): name for name in tkfont.families(self)}
        default = tkfont.nametofont("TkDefaultFont")
        family = next((families[name] for name in ("segoe ui variable text", "segoe ui variable", "segoe ui")
                       if name in families), default.actual("family"))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=family, size=10)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BACKGROUND, foreground=FOREGROUND, font="TkDefaultFont",
                        bordercolor=BORDER, focuscolor="#d989a4", lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=FOREGROUND, troughcolor=FIELD)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Field.TLabel", background=SURFACE)
        style.configure("Heading.TLabel", font=(family, 23, "bold"))
        style.configure("Subtitle.TLabel", font=(family, 12))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TButton", padding=(self.px(12), self.px(5)), background=SURFACE)
        style.configure("Accent.TButton", padding=(self.px(23), self.px(7)), background=ACCENT)
        style.map("TButton", background=[("disabled", SURFACE), ("active", BORDER)],
                  foreground=[("disabled", MUTED)])
        style.map("Accent.TButton", background=[("disabled", SURFACE), ("active", HOVER)])
        style.configure("TEntry", fieldbackground=FIELD, padding=self.px(4))
        style.configure("TCombobox", fieldbackground=FIELD, padding=self.px(4))
        for field in ("TEntry", "TCombobox"):
            style.map(field, fieldbackground=[("readonly", FIELD), ("disabled", SURFACE)],
                      foreground=[("readonly", FOREGROUND), ("disabled", MUTED)],
                      selectforeground=[("readonly", FOREGROUND)], selectbackground=[("readonly", FIELD)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FOREGROUND)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", FOREGROUND)

    def update_mode_hint(self, _event=None):
        duplicate = self.primary_lang.get() == self.secondary_lang.get()
        self.mode_hint.set("Choose two different languages." if duplicate else PRIMARY_HINT)

    def write_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.configure(state="disabled")
        self.log.see("end")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def refresh_language_lists(self, game):
        cooked = Path(game) / "CookedPC"
        available = [name for name, fn in LANGS.items() if (cooked / fn).is_file()]
        if not available:
            available = list(LANGS)
        self.primary_combo["values"] = available
        self.secondary_combo["values"] = available
        if self.primary_lang.get() not in available:
            self.primary_lang.set(available[0])
        if self.secondary_lang.get() not in available or self.secondary_lang.get() == self.primary_lang.get():
            fallback = next((x for x in available if x != self.primary_lang.get()), available[0])
            self.secondary_lang.set(fallback)
        self.update_mode_hint()

    def browse_game(self):
        initial = self.game.get().strip()
        initial_dir = initial if initial and Path(initial).is_dir() else str(Path.home())
        selected = filedialog.askdirectory(
            title="Select The Witcher 2 game folder",
            initialdir=initial_dir,
            mustexist=True,
        )
        if not selected:
            return
        game = Path(selected)
        if not is_game_folder(game):
            messagebox.showerror("Invalid game folder",
                                 "Select the game folder containing CookedPC, pack0.dzip and a supported localization.",
                                 parent=self)
            return
        self.select_game(game)

    def detect(self):
        current = self.game.get()
        self.select_game(Path(current) if current and is_game_folder(current) else find_game())

    def select_game(self, game):
        gibbed = find_gibbed(TOOLS_DIR.parent)
        self.game.set(str(game) if game else "")
        self.gibbed.set(str(gibbed) if gibbed else "")
        self.clear_log()
        if game:
            self.refresh_language_lists(game)
            self.write_log(f"Game folder: {game}")
            self.set_game_emblem(game)
        else:
            self.write_log("Game folder not found. Select it with Browse.")
            self.emblem.grid_remove()
            self.schedule_layout()
        if not gibbed:
            self.write_log("Bundled tools are missing. Extract the full ZIP with its _internal folder.")
        self.status.set("Ready." if game and gibbed else "Setup incomplete.")
        if game:
            self.check_recovery(game)

    def set_game_emblem(self, game):
        from w2_windows_ui import load_game_icon

        self.emblem.grid_remove()
        try:
            image = load_game_icon(game, self.px(36))
            if image is not None:
                self.game_emblem = tk.PhotoImage(master=self, data=image, format="PPM")
                self.iconphoto(True, self.game_emblem)
                self.emblem.configure(image=self.game_emblem)
                self.emblem.grid()
        except (OSError, tk.TclError):
            pass
        self.schedule_layout()

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.install_btn, self.restore_btn, self.browse_btn, self.rescan_btn):
            button.configure(state=state)
        for combo in (self.primary_combo, self.secondary_combo):
            combo.configure(state="disabled" if busy else "readonly")

    def close_window(self):
        if self.busy:
            messagebox.showinfo("Operation in progress", "Wait for the current operation to finish.")
        else:
            self.destroy()

    def check_recovery(self, game):
        state = Path(game) / "CookedPC" / STATE_DIR
        if state.exists():
            self.run_operation(game, "recover")

    def start_install(self):
        if not self.game.get() or not self.gibbed.get():
            self.detect()
        if self.busy:
            return
        game, gibbed = self.game.get(), self.gibbed.get()
        if not game or not gibbed:
            messagebox.showerror("Automatic detection failed", "The game or Gibbed.RED.Strings.exe was not found.")
            return
        primary, secondary = self.primary_lang.get(), self.secondary_lang.get()
        if primary == secondary or primary not in P3_LANGUAGES or secondary not in P3_LANGUAGES:
            messagebox.showerror("Language error", "Choose two different available languages.")
            return
        pair = P3_LANGUAGES[primary], P3_LANGUAGES[secondary]
        self.run_operation(game, "install", pair, Path(gibbed))

    def restore(self):
        if not self.game.get():
            self.detect()
        if self.busy:
            return
        if not self.game.get():
            messagebox.showerror("Game not found", "Select the game folder first.")
            return
        self.run_operation(self.game.get(), "restore")

    def run_operation(self, game, action, pair=None, gibbed=None):
        if self.busy:
            return
        self.set_busy(True)
        self.status.set({"install": "Preparing localization and verifying glyphs...",
                         "restore": "Checking backups...",
                         "recover": "Checking recovery journal..."}[action])
        if action != "recover":
            self.clear_log()
        threading.Thread(
            target=self.worker, args=(game, action, pair, gibbed),
            daemon=True,
        ).start()
        self.after(50, self.poll_operation)

    def worker(self, game, action, pair, gibbed):
        def ask(title, question):
            response = queue.Queue(maxsize=1)
            self.events.put(("ask", (title, question, response)))
            return response.get()

        def approve_sources(sources):
            details = "\n".join(f"  {name} ({reason})" for name, reason in sources.items())
            return ask(
                "Confirm clean localization files",
                f"{details}\n\nThe file structure has been verified. Confirm these are clean game "
                "localizations from Steam/GOG or another verified game source. If unsure, verify "
                "the game files first.\n\nUse these files as the original sources?",
            )

        def approve_restore(names):
            return ask("Restore originals", "Restore these slots for the selected game folder?\n\n"
                       + "\n".join(names))

        try:
            from w2_font_test import FontTransaction, ensure_game_closed, installed_game, run_probe
            state = GameState(installed_game(game))
            with state.locked(create=action == "install") as locked:
                font = FontTransaction(state)
                font_state = font.load() if locked else None
                if font_state is not None:
                    if action == "restore":
                        font.no_regular_install()
                        names = tuple(font_state["files"])
                        if approve_restore(names):
                            ensure_game_closed()
                            font.restore()
                            status, detail = "Originals restored.", "\n".join(names)
                        else:
                            status, detail = "No restore applied.", ""
                    elif action == "recover":
                        status, detail = "Font installation journal found.", "Use Restore to remove or recover the active installation."
                    else:
                        raise StateError("Restore the active font installation before selecting a new pair.")
                elif locked and state.recover():
                    status = "Interrupted operation rolled back."
                    detail = status if action == "recover" else status + " Run the requested operation again."
                elif action == "install":
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="ExiZz-W2-",
                                                     suffix=".json", delete=False) as report_file:
                        report = run_probe(state, pair, TOOLS_DIR, apply=True,
                                           coverage_report=report_file, approve=approve_sources)
                    if report is None:
                        status, detail = "Cancelled.", ""
                    else:
                        skipped = len(report["skipped_glyph_records"])
                        status = "Installed successfully." if not skipped else "Installed with coverage exceptions."
                        primary = next(name for name, code in P3_LANGUAGES.items() if code == pair[0])
                        detail = (f"{len(report['changed_ids']):,} / {report['p1_eligible']:,} eligible records. "
                                  f"{skipped} kept original due to coverage.\nText Language: {primary}. "
                                  f"\nCoverage report: {report_file.name}")
                elif action == "restore":
                    restored = state.restore(approve_restore) if locked else ()
                    status = "Originals restored." if restored else "No restore applied."
                    detail = "\n".join(restored)
                else:
                    status, detail = "Ready.", ""
            self.events.put(("done", (status, detail, action)))
        except Exception as error:
            self.events.put(("error", str(error)))

    def poll_operation(self):
        try:
            event, payload = self.events.get_nowait()
        except queue.Empty:
            self.after(50, self.poll_operation)
            return
        if event == "ask":
            title, question, response = payload
            response.put(messagebox.askyesno(title, question, parent=self))
            self.after(50, self.poll_operation)
            return
        self.set_busy(False)
        if event == "error":
            self.status.set("Error.")
            self.write_log(payload)
            messagebox.showerror("Operation failed", payload, parent=self)
            return
        status, detail, action = payload
        self.status.set(status)
        if detail:
            self.write_log(detail)
        if action in ("install", "font_install") and status == "Installed successfully.":
            messagebox.showinfo(APP_TITLE, detail, parent=self)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--cli", "--p2", "--p3"):
        from w2_font_test import main
        sys.exit(main(sys.argv[2:]))
    if len(sys.argv) == 3 and sys.argv[1] == "--runtime-check":
        from w2_runtime import write_runtime_report
        sys.exit(write_runtime_report(Path(sys.argv[2])))
    from w2_windows_ui import enable_system_dpi
    enable_system_dpi()
    App().mainloop()

