# -*- coding: utf-8 -*-
"""
The Witcher 2 — Universal Dual Subtitles Tool v2.1

Final automatic installer for the proven W2-safe merge mode:
    PRIMARY + " ~ " + SECONDARY

Default pair:
    English (en0.w2strings) + Polish (pl0.w2strings)

The tool automatically:
- shows only language files that actually exist in your installed CookedPC;
- detects the Steam installation of The Witcher 2 (AppID 20920);
- finds the selected language files in CookedPC;
- finds Gibbed.RED.Strings.exe in common locations or next to this tool;
- creates a permanent clean backup before the first installation;
- restores that clean backup before every rebuild, preventing duplicate subtitles;
- decodes -> merges matching text IDs -> encodes;
- installs the generated language file directly into the game;
- can restore the clean backup with one click;
- includes a built-in guide for standard, Chinese, and Japanese setups.

Important: the real-newline experiment is NOT used here. The confirmed
W2-safe separator is " ~ ". The tool intentionally does not modify font files.
For Chinese/Japanese, the game Text Language must match the selected CJK language.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from xml.etree import ElementTree as ET

APP_TITLE = "The Witcher 2 — Universal Dual Subtitles"
APP_VERSION = "v2.2 FINAL"
CREATOR_NICK = "ExiZz"
STEAM_APPID = "20920"
BACKUP_DIR_NAME = "_DualSubtitles_Backups"
BACKUP_NAME_TEMPLATE = "{}.original"

# Proven working W2-safe separator from v1.3.
SEPARATOR = " ~ "

LANGS = {
    "English": "en0.w2strings",
    "Polish": "pl0.w2strings",
    "Russian": "ru0.w2strings",
    "German": "de0.w2strings",
    "French": "fr0.w2strings",
    "Spanish": "es0.w2strings",
    "Italian": "it0.w2strings",
    "Czech": "cz0.w2strings",
    "Hungarian": "hu0.w2strings",
    "Japanese": "jp0.w2strings",
    "Turkish": "tr0.w2strings",
    "Chinese": "zh0.w2strings",
}


def run_gibbed(exe, args, cwd=None):
    p = subprocess.run([str(exe)] + list(map(str, args)), cwd=cwd,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(
            "Gibbed RED Tools failed.\n\n" + (p.stdout or "") + "\n" + (p.stderr or "")
        )
    return p.stdout + "\n" + p.stderr


def decode(exe, src, work):
    src = Path(src).resolve()
    out_xml = Path(work) / (src.stem + ".xml")
    run_gibbed(exe, ["-d", src, out_xml])
    if not out_xml.is_file():
        beside = src.with_suffix(".xml")
        if beside.is_file():
            shutil.copy2(beside, out_xml)
        else:
            raise RuntimeError(f"Gibbed decoded {src.name}, but XML output was not found.")
    return out_xml


def encode(exe, xml, out_w2strings):
    out_w2strings = Path(out_w2strings).resolve()
    run_gibbed(exe, ["-e", str(xml), str(out_w2strings)])
    if not out_w2strings.is_file() or out_w2strings.stat().st_size == 0:
        beside = Path(xml).with_suffix(".w2strings")
        if beside.is_file() and beside.resolve() != out_w2strings:
            shutil.copy2(beside, out_w2strings)
        else:
            raise RuntimeError("Gibbed did not create the encoded .w2strings file.")



# Turkish compatibility mode:
# The original Gibbed RED Tools build bundled with this package does not know
# the Turkish tr0.w2strings file key and throws NotSupportedException before
# decoding it. We therefore implement the same W2Strings binary format here
# for Turkish only. The constants below were verified against the stock tr0
# file and its shared key table.
TR_W2_MAGIC = 0x21795135


def _read_s32(f):
    op_b = f.read(1)
    if not op_b:
        raise RuntimeError("Unexpected end of .w2strings file.")
    op = op_b[0]
    value = op & 0x3F
    if op & 0x40:
        shift = 6
        while True:
            extra_b = f.read(1)
            if not extra_b:
                raise RuntimeError("Unexpected end of encoded integer.")
            extra = extra_b[0]
            value |= (extra & 0x7F) << shift
            shift += 7
            if not (extra & 0x80):
                break
    return -value if (op & 0x80) else value


def _write_s32(value):
    negative = value <= 0
    if negative:
        value = -value
    op = value & 0x3F
    if negative:
        op |= 0x80
    value >>= 6
    if value:
        op |= 0x40
    out = bytearray([op])
    while value:
        extra = value & 0x7F
        value >>= 7
        if value:
            extra |= 0x80
        out.append(extra)
    return bytes(out)


def _read_encoded_string(f):
    length = _read_s32(f)
    if length < 0:
        raw = f.read(-length)
        if len(raw) != -length:
            raise RuntimeError("Unexpected end of encoded string.")
        return raw.decode("cp1252", errors="replace")
    raw = f.read(length * 2)
    if len(raw) != length * 2:
        raise RuntimeError("Unexpected end of Unicode encoded string.")
    return raw.decode("utf-16le", errors="replace")


def _write_encoded_string(value):
    # Match Gibbed.RED.FileFormats.StreamHelpers.WriteEncodedString().
    is_unicode = any(ord(ch) > 0xFF and ch != "…" for ch in value)
    if not is_unicode:
        value = value.replace("…", "...")
        raw = value.encode("cp1252", errors="replace")
        return _write_s32(-len(value)) + raw
    raw = value.encode("utf-16le", errors="replace")
    return _write_s32(len(value)) + raw


def _rol16(value):
    return ((value << 1) | (value >> 15)) & 0xFFFF


def _decrypt_w2_text(raw, magic, version):
    if magic == 0:
        return raw.decode("utf-16le", errors="replace")
    buf = bytearray(raw)
    string_key = (magic >> 8) & 0xFFFF
    for j in range(0, len(buf), 2):
        if version >= 200:
            char_key = (((len(buf) // 2) + 1) * string_key) & 0xFFFF
            buf[j] ^= char_key & 0xFF
            buf[j + 1] ^= (char_key >> 8) & 0xFF
            string_key = _rol16(string_key)
        else:
            buf[j] ^= string_key & 0xFF
            buf[j + 1] ^= (string_key >> 8) & 0xFF
            string_key = (string_key + 1) & 0xFFFF
    return bytes(buf).decode("utf-16le", errors="replace")


def _encrypt_w2_text(value, magic, version):
    buf = bytearray(value.encode("utf-16le", errors="replace"))
    if magic == 0:
        return bytes(buf)
    string_key = (magic >> 8) & 0xFFFF
    for j in range(0, len(buf), 2):
        if version >= 200:
            char_key = (((len(buf) // 2) + 1) * string_key) & 0xFFFF
            buf[j] ^= char_key & 0xFF
            buf[j + 1] ^= (char_key >> 8) & 0xFF
            string_key = _rol16(string_key)
        else:
            buf[j] ^= string_key & 0xFF
            buf[j + 1] ^= (string_key >> 8) & 0xFF
            string_key = (string_key + 1) & 0xFFFF
    return bytes(buf)


def _read_turkish_native(src):
    import struct
    from collections import OrderedDict
    with open(src, "rb") as f:
        version = struct.unpack("<I", f.read(4))[0]
        encryption_key = 0
        if version >= 114:
            encryption_key |= struct.unpack("<H", f.read(2))[0] << 16
        key_count = _read_s32(f)
        keys = OrderedDict()
        for _ in range(key_count):
            key = _read_encoded_string(f)
            value = struct.unpack("<I", f.read(4))[0] ^ TR_W2_MAGIC
            keys[key] = value
        if version >= 114:
            encryption_key |= struct.unpack("<H", f.read(2))[0]
        magic = TR_W2_MAGIC
        stored_hash = struct.unpack("<I", f.read(4))[0] ^ magic if version >= 200 else None
        text_count = _read_s32(f)
        texts = OrderedDict()
        computed_hash = 0
        for _ in range(text_count):
            index = struct.unpack("<I", f.read(4))[0] ^ magic
            length = _read_s32(f)
            if length < 0:
                ascii_raw = f.read(-length)
                if len(ascii_raw) != -length:
                    raise RuntimeError("Unexpected end of Turkish string data.")
                raw = bytearray(len(ascii_raw) * 2)
                for i, b in enumerate(ascii_raw):
                    raw[i * 2] = b
            else:
                raw = bytearray(f.read(length * 2))
                if len(raw) != length * 2:
                    raise RuntimeError("Unexpected end of Turkish string data.")
            for j in range(0, len(raw), 2):
                computed_hash = (computed_hash + int.from_bytes(raw[j:j + 2], "little")) & 0xFFFFFFFF
            texts[index] = _decrypt_w2_text(bytes(raw), magic, version)
    return version, encryption_key, keys, texts, stored_hash, computed_hash


def _write_turkish_native(path, version, encryption_key, keys, texts):
    import struct
    magic = TR_W2_MAGIC
    out = bytearray(struct.pack("<I", version))
    if version >= 114:
        out += struct.pack("<H", (encryption_key >> 16) & 0xFFFF)
    out += _write_s32(len(keys))
    for key, value in keys.items():
        out += _write_encoded_string(key)
        out += struct.pack("<I", value ^ magic)
    if version >= 114:
        out += struct.pack("<H", encryption_key & 0xFFFF)
    hash_pos = None
    if version >= 200:
        hash_pos = len(out)
        out += b"\x00\x00\x00\x00"
    out += _write_s32(len(texts))
    strings_hash = 0
    for index, value in texts.items():
        out += struct.pack("<I", index ^ magic)
        raw = _encrypt_w2_text(value, magic, version)
        for j in range(0, len(raw), 2):
            strings_hash = (strings_hash + int.from_bytes(raw[j:j + 2], "little")) & 0xFFFFFFFF
        out += _write_s32(len(raw) // 2)
        out += raw
    if version >= 200:
        out[hash_pos:hash_pos + 4] = struct.pack("<I", strings_hash ^ magic)
    elif version >= 114:
        out += struct.pack("<I", strings_hash)
    Path(path).write_bytes(out)


def _xml_from_texts(texts, out_xml):
    root = ET.Element("strings", {"version": "201", "encryption_key": "0"})
    texts_el = ET.SubElement(root, "texts")
    for index, value in texts.items():
        el = ET.SubElement(texts_el, "text", {"id": str(index)})
        el.text = value
    ET.ElementTree(root).write(out_xml, encoding="utf-8", xml_declaration=True)


def merge_turkish_native(primary, secondary_xml, secondary_is_turkish, secondary_path, output_w2strings):
    version, encryption_key, keys, ptexts, _, _ = _read_turkish_native(primary)
    if secondary_is_turkish:
        _, _, _, stexts, _, _ = _read_turkish_native(secondary_path)
    else:
        _, stexts_xml = load_xml(secondary_xml)
        stexts = {int(k): (v.text or "") for k, v in stexts_xml.items() if k.isdigit()}

    changed = missing = empty = already = 0
    total = len(ptexts)
    for tid, p in list(ptexts.items()):
        if not p.strip():
            continue
        s = stexts.get(tid)
        if s is None:
            missing += 1
            continue
        if not str(s).strip():
            empty += 1
            continue
        s = str(s)
        if s == p or (" ~ " + s) in p:
            already += 1
            continue
        ptexts[tid] = p + SEPARATOR + s
        changed += 1
    _write_turkish_native(output_w2strings, version, encryption_key, keys, ptexts)
    return changed, missing, empty, already, total


def make_turkish_xml(src, out_xml):
    _, _, _, texts, _, _ = _read_turkish_native(src)
    root = ET.Element("strings", {"version": "201", "encryption_key": "0"})
    texts_el = ET.SubElement(root, "texts")
    for index, value in texts.items():
        el = ET.SubElement(texts_el, "text", {"id": str(index)})
        el.text = value
    ET.ElementTree(root).write(out_xml, encoding="utf-8", xml_declaration=True)


def xml_char_ok(cp):
    return cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


def sanitize_xml_text(raw):
    def fix_ref(m):
        token = m.group(0)
        try:
            if token.lower().startswith("&#x"):
                cp = int(token[3:-1], 16)
            else:
                cp = int(token[2:-1], 10)
            return chr(cp) if xml_char_ok(cp) else ""
        except Exception:
            return ""
    raw = re.sub(r"&#(?:x[0-9A-Fa-f]+|[0-9]+);", fix_ref, raw)
    return "".join(ch for ch in raw if xml_char_ok(ord(ch)))


def load_xml(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = sanitize_xml_text(f.read())
    tree = ET.ElementTree(ET.fromstring(raw))
    root = tree.getroot()
    texts = {}
    for el in root.iter("text"):
        tid = el.get("id")
        if tid is not None:
            texts[tid] = el
    return tree, texts


def merge_xml(primary_xml, secondary_xml, output_xml):
    tree_p, ptexts = load_xml(primary_xml)
    _, stexts = load_xml(secondary_xml)

    changed = missing = empty = already = 0
    for tid, pel in ptexts.items():
        p = pel.text or ""
        if not p.strip():
            continue
        sel = stexts.get(tid)
        if sel is None:
            missing += 1
            continue
        s = sel.text or ""
        if not s.strip():
            empty += 1
            continue

        # W2-safe format. No <br> and no real XML newline: both have caused
        # renderer/encoder problems in different W2 UI paths.
        if s == p or s in p and (" ~ " + s) in p:
            already += 1
            continue

        pel.text = p + SEPARATOR + s
        changed += 1

    tree_p.write(output_xml, encoding="utf-8", xml_declaration=True)
    return changed, missing, empty, already, len(ptexts)


def is_cjk_language(name):
    return name in ("Chinese", "Japanese")


def merge_cjk_target(primary_xml, cjk_xml, output_xml):
    """Store Primary ~ CJK in the CJK file so W2 uses the CJK font path."""
    tree_cjk, ctexts = load_xml(cjk_xml)
    _, ptexts = load_xml(primary_xml)
    changed = missing = empty = already = 0
    for tid, cel in ctexts.items():
        c = cel.text or ""
        if not c.strip():
            empty += 1
            continue
        pel = ptexts.get(tid)
        if pel is None:
            missing += 1
            continue
        p = pel.text or ""
        if not p.strip():
            continue
        if (SEPARATOR + c) in c or (p + SEPARATOR + c) == c:
            already += 1
            continue
        cel.text = p + SEPARATOR + c
        changed += 1
    tree_cjk.write(output_xml, encoding="utf-8", xml_declaration=True)
    return changed, missing, empty, already, len(ctexts)


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
    for p in candidates:
        cooked = p / "CookedPC"
        if (cooked / "en0.w2strings").is_file():
            return p
    return None


def find_gibbed(tool_dir):
    # v2.1 bundles the complete Gibbed RED Tools package with the installer.
    # Prefer the bundled copy so the user does not need to install or select anything.
    bundled = Path(tool_dir) / "Gibbed RED Tools" / "Gibbed.RED.Strings.exe"
    direct = Path(tool_dir) / "Gibbed.RED.Strings.exe"
    candidates = [
        bundled,
        direct,
        Path(r"C:/Program Files/Gibbed's RED Tools/Gibbed.RED.Strings.exe"),
        Path(r"C:/Program Files (x86)/Gibbed's RED Tools/Gibbed.RED.Strings.exe"),
    ]
    home = Path.home()
    for base in [home / "Downloads", home / "Desktop", home / "Documents"]:
        if base.is_dir():
            try:
                candidates.extend(base.glob("Gibbed.RED.Strings.exe"))
                candidates.extend(base.glob("**/Gibbed.RED.Strings.exe"))
            except Exception:
                pass
    for p in candidates:
        if p.is_file():
            return p
    return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE + " " + APP_VERSION)
        self.geometry("900x700")
        self.minsize(820, 620)
        self.resizable(True, True)

        self.game = tk.StringVar()
        self.gibbed = tk.StringVar()
        self.primary_lang = tk.StringVar(value="English")
        self.secondary_lang = tk.StringVar(value="Polish")
        self.status = tk.StringVar(value="Detecting game...")
        self.mode_hint = tk.StringVar(value="Choose Primary and Secondary, then press GUIDE if you are using Chinese or Japanese.")
        self.primary_combo = None
        self.secondary_combo = None

        pad = {"padx": 12, "pady": 7}
        ttk.Label(self, text=APP_TITLE + " " + APP_VERSION, font=("Segoe UI", 16, "bold")).pack(anchor="w", **pad)
        ttk.Label(self, text="Automatic mode: detects the Steam game, uses the bundled Gibbed tools, backs up the original, builds the dual file and installs it directly.", wraplength=780).pack(anchor="w", **pad)
        ttk.Label(self, text="Created by " + CREATOR_NICK, font=("Segoe UI", 9, "italic")).pack(anchor="w", padx=12, pady=(0, 2))

        frm = ttk.Frame(self)
        frm.pack(fill="x", **pad)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Game folder").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.game, state="readonly").grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse...", command=self.browse_game).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(frm, text="Rescan", command=self.detect).grid(row=0, column=3)

        ttk.Label(frm, text="Primary language").grid(row=1, column=0, sticky="w", pady=6)
        self.primary_combo = ttk.Combobox(frm, textvariable=self.primary_lang, values=list(LANGS), state="readonly")
        self.primary_combo.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="shown first / game file replaced").grid(row=1, column=3, sticky="w")

        ttk.Label(frm, text="Secondary language").grid(row=2, column=0, sticky="w", pady=6)
        self.secondary_combo = ttk.Combobox(frm, textvariable=self.secondary_lang, values=list(LANGS), state="readonly")
        self.secondary_combo.grid(row=2, column=1, sticky="w", padx=6)
        self.primary_combo.bind("<<ComboboxSelected>>", lambda e: self.update_mode_hint())
        self.secondary_combo.bind("<<ComboboxSelected>>", lambda e: self.update_mode_hint())
        ttk.Label(frm, text="added after ~").grid(row=2, column=3, sticky="w")

        self.mode_label = ttk.Label(self, textvariable=self.mode_hint, wraplength=850, justify="left")
        self.mode_label.pack(anchor="w", padx=12, pady=(0, 7))

        info = ttk.LabelFrame(self, text="Automatic protection")
        info.pack(fill="x", **pad)
        ttk.Label(info, text="Before the first install, the original primary file is copied to CookedPC\\_DualSubtitles_Backups. Every rebuild starts from that clean backup, so subtitles are never duplicated.", wraplength=780).pack(anchor="w", padx=10, pady=8)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **pad)
        self.install_btn = ttk.Button(buttons, text="INSTALL DUAL SUBTITLES", command=self.start_install)
        self.install_btn.pack(side="left")
        self.restore_btn = ttk.Button(buttons, text="RESTORE ORIGINAL", command=self.restore)
        self.restore_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="Open CookedPC", command=self.open_cooked).pack(side="left")
        ttk.Button(buttons, text="GUIDE", command=self.show_guide).pack(side="left", padx=8)

        logbox = ttk.LabelFrame(self, text="Log")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, wrap="word", height=16)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Label(self, textvariable=self.status).pack(anchor="w", **pad)

        self.after(100, self.detect)

    def update_mode_hint(self):
        p = self.primary_lang.get()
        s = self.secondary_lang.get()
        if p in ("Chinese", "Japanese") and s in ("Chinese", "Japanese"):
            self.mode_hint.set("CJK + CJK is not supported together. Choose one CJK language and one non-CJK language.")
        elif p == "Chinese" or s == "Chinese":
            self.mode_hint.set("Chinese selected: set the game's Text Language to Chinese before launching. The tool does not modify font files.")
        elif p == "Japanese" or s == "Japanese":
            self.mode_hint.set("Japanese selected: set the game's Text Language to Japanese before launching. The tool does not modify font files.")
        else:
            self.mode_hint.set("Standard mode: the game Text Language should match the Primary language. Use GUIDE for details.")

    def show_guide(self):
        win = tk.Toplevel(self)
        win.title("The Witcher 2 — Dual Subtitles Guide")
        win.geometry("820x650")
        win.minsize(720, 560)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        def tab(title, text):
            frame = ttk.Frame(nb)
            nb.add(frame, text=title)
            box = tk.Text(frame, wrap="word", padx=12, pady=12)
            box.pack(fill="both", expand=True)
            box.insert("1.0", text)
            box.config(state="disabled")
            return frame

        tab("Quick start", """QUICK START

1. Select Primary language — it is shown first.
2. Select Secondary language — it is added after  ~ .
3. Click INSTALL DUAL SUBTITLES.
4. Launch The Witcher 2 and use the Text Language required by the selected languages.

The tool creates a clean backup of the file it actually replaces and starts every rebuild from that backup. This prevents duplicate subtitles.

Examples:
English + Polish -> English Text Language
English + Russian -> English Text Language
English + Chinese -> Chinese Text Language
English + Japanese -> Japanese Text Language

The important rule for Chinese/Japanese:
The game must use the CJK language path. Do NOT leave the game on English when Chinese/Japanese is part of the dual subtitle pair.

The tool does not install or replace font files automatically.
""")

        tab("Chinese", """CHINESE DUAL SUBTITLES

If Chinese is Primary:
  Primary = Chinese
  Secondary = English (or another non-CJK language)
  Game Text Language = Chinese

If Chinese is Secondary:
  Primary = English (or another non-CJK language)
  Secondary = Chinese
  Game Text Language = Chinese

The resulting text keeps the selected order:
Primary ~ Secondary

Example:
English + Chinese
  NEW GAME ~ 新游戏

The Chinese game language is intentional: Witcher 2 uses a different CJK font path for Chinese. If you keep Text Language = English, Chinese glyphs can appear blank unless you have a separate compatible CJK font modification installed.
""")

        tab("Japanese", """JAPANESE DUAL SUBTITLES

If Japanese is Primary:
  Primary = Japanese
  Secondary = English (or another non-CJK language)
  Game Text Language = Japanese

If Japanese is Secondary:
  Primary = English (or another non-CJK language)
  Secondary = Japanese
  Game Text Language = Japanese

The resulting text keeps the selected order:
Primary ~ Secondary

Example:
English + Japanese
  NEW GAME ~ 新しいゲーム

The Japanese game language is intentional: Witcher 2 uses a separate CJK font path. If you keep Text Language = English, Japanese glyphs can appear blank unless you have a compatible CJK font modification installed.
""")

        tab("Other languages", """STANDARD / NON-CJK LANGUAGES

For languages other than Chinese and Japanese, the tool uses the selected Primary language file.

Examples:
English + Polish -> game Text Language = English
English + German -> game Text Language = English
Polish + English -> game Text Language = Polish

The Primary language is always shown first and the Secondary language is added after  ~ .
""")

        tab("Restore & troubleshooting", """RESTORE ORIGINAL

RESTORE ORIGINAL returns the clean backup for the file used by the current language pair.

If text disappears:
1. Click RESTORE ORIGINAL.
2. Check that the game's Text Language matches the guide for your pair.
3. For Chinese/Japanese, do not assume that English Text Language can render CJK glyphs without a compatible CJK font mod.
4. If the game UI itself becomes blank, restore the original game files or verify the game files through Steam.

The tool never modifies fonts.swf, fonts_zh.swf, fonts_jp.swf, cfonts.swf, or fonts.csv.

Created by ExiZz.
""")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    def write_log(self, s):
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.update_idletasks()

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
        cooked = game / "CookedPC"
        if not cooked.is_dir() or not (cooked / "en0.w2strings").is_file():
            messagebox.showerror(
                "Invalid game folder",
                "This does not look like The Witcher 2 game folder.\n\n"
                "Select the main game folder that contains the CookedPC folder and en0.w2strings."
            )
            return
        self.game.set(str(game))
        self.refresh_language_lists(game)
        gibbed = find_gibbed(Path(__file__).resolve().parent)
        self.gibbed.set(str(gibbed) if gibbed else "")
        self.log.delete("1.0", "end")
        self.write_log("GAME SELECTED: " + str(game))
        self.write_log("CookedPC: " + str(cooked))
        if gibbed:
            self.write_log("GIBBED FOUND: " + str(gibbed))
            if str(gibbed).lower().startswith(str(Path(__file__).resolve().parent).lower()):
                self.write_log("Bundled Gibbed RED Tools: YES")
        else:
            self.write_log("GIBBED NOT FOUND.")
        self.update_mode_hint()
        self.status.set("Ready." if gibbed else "Game selected; Gibbed tools not found.")

    def detect(self):
        game = find_game()
        gibbed = find_gibbed(Path(__file__).resolve().parent)
        self.game.set(str(game) if game else "")
        self.gibbed.set(str(gibbed) if gibbed else "")
        self.log.delete("1.0", "end")
        if game:
            self.refresh_language_lists(game)
            self.write_log("GAME FOUND: " + str(game))
            self.write_log("CookedPC: " + str(game / "CookedPC"))
        else:
            self.write_log("GAME NOT FOUND. Use Rescan after installing Steam/game.")
        if gibbed:
            self.write_log("GIBBED FOUND: " + str(gibbed))
            if str(gibbed).lower().startswith(str(Path(__file__).resolve().parent).lower()):
                self.write_log("Bundled Gibbed RED Tools: YES")
        else:
            self.write_log("GIBBED NOT FOUND.")
            self.write_log("The bundled Gibbed RED Tools should be included with v1.6.")
            self.write_log("If missing, extract the complete v2.1 package again.")
        self.update_mode_hint()
        self.status.set("Ready." if game and gibbed else "Automatic detection incomplete.")

    def paths(self):
        game = Path(self.game.get())
        cooked = game / "CookedPC"
        selected_primary = cooked / LANGS[self.primary_lang.get()]
        selected_secondary = cooked / LANGS[self.secondary_lang.get()]
        # If CJK is Secondary, write to the CJK file so Witcher 2 uses the CJK
        # language/font path. Otherwise the selected Primary file is the target.
        target = selected_secondary if is_cjk_language(self.secondary_lang.get()) else selected_primary
        backup_dir = cooked / BACKUP_DIR_NAME
        backup = backup_dir / BACKUP_NAME_TEMPLATE.format(target.name)
        return game, cooked, target, selected_primary, selected_secondary, backup

    def open_cooked(self):
        try:
            _, cooked, _, _, _, _ = self.paths()
            if cooked.is_dir():
                os.startfile(cooked)
            else:
                messagebox.showerror("Not found", "CookedPC was not found.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def set_buttons(self, state):
        self.install_btn.config(state=state)
        self.restore_btn.config(state=state)

    def start_install(self):
        if not self.game.get() or not self.gibbed.get():
            self.detect()
        if not self.game.get() or not self.gibbed.get():
            messagebox.showerror("Automatic detection failed", "The game or Gibbed.RED.Strings.exe was not found. Put Gibbed.RED.Strings.exe next to this tool and press Rescan.")
            return
        if self.primary_lang.get() == self.secondary_lang.get():
            messagebox.showerror("Language error", "Primary and secondary languages must be different.")
            return
        self.set_buttons("disabled")
        self.status.set("Installing...")
        self.log.delete("1.0", "end")
        threading.Thread(target=self.worker_install, daemon=True).start()

    def worker_install(self):
        try:
            game, cooked, target, selected_primary, secondary, backup = self.paths()
            primary_name = self.primary_lang.get()
            secondary_name = self.secondary_lang.get()
            primary = selected_primary
            cjk_secondary = is_cjk_language(secondary_name)
            cjk_primary = is_cjk_language(primary_name)

            if not cooked.is_dir():
                raise RuntimeError("CookedPC folder not found: " + str(cooked))
            if not primary.is_file():
                raise RuntimeError(f"Primary language file not found: {primary.name}")
            if not secondary.is_file():
                raise RuntimeError(f"Secondary language file not found: {secondary.name}")
            if primary_name == secondary_name:
                raise RuntimeError("Primary and secondary languages must be different.")
            if cjk_primary and cjk_secondary:
                raise RuntimeError("Chinese + Japanese dual subtitles are not supported together. Choose one CJK language and one non-CJK language.")
            gibbed = Path(self.gibbed.get())
            if not gibbed.is_file():
                raise RuntimeError("Gibbed.RED.Strings.exe not found.")

            self.write_log("=== AUTOMATIC INSTALL ===")
            self.write_log("Game: " + str(game))
            self.write_log("Primary: " + primary.name)
            self.write_log("Secondary: " + secondary.name)
            self.write_log("Target: " + target.name)
            self.write_log("Separator: ' ~ ' (v1.3 confirmed working mode)")

            if cjk_primary or cjk_secondary:
                game_cjk = primary_name if cjk_primary else secondary_name
                self.write_log("CJK mode: YES")
                self.write_log("Game Text Language MUST be: " + game_cjk)
                self.write_log("Font files are NOT modified by this tool.")
            else:
                self.write_log("CJK mode: NO")
                self.write_log("Game Text Language: use the selected Primary language.")

            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.is_file():
                shutil.copy2(target, backup)
                self.write_log("Original target backup created: " + str(backup))
            else:
                self.write_log("Clean target backup already exists.")

            # Always start from the clean target to prevent duplicate subtitles.
            shutil.copy2(backup, target)
            self.write_log("Restored clean target before rebuild.")

            with tempfile.TemporaryDirectory(prefix="w2dual_auto_") as work:
                merged_xml = Path(work) / "merged.xml"
                primary_is_tr = primary.name.lower() == "tr0.w2strings"
                secondary_is_tr = secondary.name.lower() == "tr0.w2strings"

                if (cjk_primary or cjk_secondary) and (primary_is_tr or secondary_is_tr):
                    raise RuntimeError("CJK mode cannot use Turkish as the other language.")

                # Turkish is a special native format: Gibbed.RED.Strings.exe
                # throws NotSupportedException on tr0.w2strings. Do NOT decode
                # the Turkish primary through Gibbed; the native merger below
                # reads/writes it directly.
                if primary_is_tr:
                    pxml = None
                    self.write_log("1/3 Decode primary: native Turkish compatibility mode (Gibbed skipped)")
                else:
                    self.write_log("1/3 Decode primary...")
                    pxml = decode(gibbed, primary, work)
                    self.write_log("    Primary decoder: Gibbed RED Tools")

                self.write_log("2/3 Decode secondary...")
                if secondary_is_tr:
                    sxml = Path(work) / "tr0_native.xml"
                    make_turkish_xml(secondary, sxml)
                    self.write_log("    Secondary decoder: native Turkish compatibility mode")
                else:
                    sxml = decode(gibbed, secondary, work)
                    self.write_log("    Secondary decoder: Gibbed RED Tools")

                if cjk_secondary:
                    changed, missing, empty, already, total = merge_cjk_target(pxml, sxml, merged_xml)
                    merge_description = "Primary ~ CJK stored in the CJK language file"
                elif primary_is_tr:
                    tmp_out = Path(work) / target.name
                    changed, missing, empty, already, total = merge_turkish_native(
                        primary, sxml, secondary_is_tr, secondary, tmp_out
                    )
                    shutil.copy2(tmp_out, target)
                    merge_description = "Primary Turkish target"
                else:
                    changed, missing, empty, already, total = merge_xml(pxml, sxml, merged_xml)
                    merge_description = "Primary target"

                self.write_log("Merge mode: " + merge_description)
                self.write_log(f"Primary text records: {total}")
                self.write_log(f"Dual records created: {changed}")
                self.write_log(f"Secondary ID missing: {missing}")
                self.write_log(f"Secondary text empty: {empty}")
                self.write_log(f"Already bilingual: {already}")

                if not primary_is_tr:
                    tmp_out = Path(work) / target.name
                    self.write_log("3/3 Encode...")
                    encode(gibbed, merged_xml, tmp_out)
                    self.write_log("    Encoder: Gibbed RED Tools")
                    if tmp_out.stat().st_size == 0:
                        raise RuntimeError("Encoded file is empty; original game file was kept/restored.")
                    shutil.copy2(tmp_out, target)
                else:
                    self.write_log("3/3 Encode: native Turkish compatibility mode")

            self.write_log("INSTALLED: " + str(target))
            self.status.set("Installed successfully.")
            game_language = primary_name if not (cjk_primary or cjk_secondary) else (primary_name if cjk_primary else secondary_name)
            self.write_log("GAME TEXT LANGUAGE: " + game_language)
            messagebox.showinfo(
                "Witcher 2 Dual Subtitles",
                f"Done!\n\n{primary_name} + {secondary_name} dual subtitles are installed.\n\n"
                f"Game Text Language: {game_language}\n\n"
                f"The original {target.name} is safely backed up in _DualSubtitles_Backups."
            )
        except Exception as e:
            self.status.set("Error.")
            self.write_log("\nERROR:\n" + str(e))
            messagebox.showerror("Installation failed", str(e))
        finally:
            self.set_buttons("normal")

    def restore(self):
        try:
            if not self.game.get():
                self.detect()
            _, _, target, _, _, backup = self.paths()
            if not backup.is_file():
                messagebox.showwarning("No backup", "No clean backup was created by this tool yet.")
                return
            if not messagebox.askyesno(
                "Restore original",
                f"Restore the original {target.name}?\n\nThis will remove the dual subtitles."
            ):
                return
            shutil.copy2(backup, target)
            self.write_log("RESTORED ORIGINAL: " + str(target))
            self.status.set("Original restored.")
            messagebox.showinfo("Restored", f"Original {target.name} restored.")
        except Exception as e:
            messagebox.showerror("Restore failed", str(e))


if __name__ == "__main__":
    App().mainloop()
