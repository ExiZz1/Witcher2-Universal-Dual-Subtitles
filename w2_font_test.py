"""In-place CJK font probe for The Witcher 2."""
import argparse
import csv
import ctypes
from ctypes import wintypes
import io
from html import unescape
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tempfile
from itertools import permutations
from time import perf_counter
import unicodedata

from w2_codec import Reader, assemble_changes, decode_source, inspect_table, plan_merge, reconcile_keys
from w2_languages import LOCALES
from w2_install import LEGACY_DIR, GameState, StateError, checked_path, digest, read_file
from w2_swf import Bits, FontError, Glyph, has_outline, ink_bounds, library_faces, patch_library, read_movie, shape_geometry, visible_codes


FONT_PROFILES = {
    "default": ("fonts.swf", "FontName"),
    "jp": ("fonts_jp.swf", "FontName_JP"),
    "zh": ("fonts_zh.swf", "FontName_ZH"),
}
PROFILES = {locale.stem[:2].upper(): (locale.stem, *FONT_PROFILES[locale.font_profile])
            for locale in LOCALES.values() if locale.codec_verified}
P3_LANGUAGES = {name: locale.stem[:2].upper() for name, locale in LOCALES.items() if locale.codec_verified}
GUI = PurePosixPath("globals/gui")
TEST_PAIRS = (("PL", "JP"), ("JP", "PL"))
PAIRS = frozenset(permutations(PROFILES, 2))
DONOR_ALIAS = "Font_Style_Standard"
HTML_TAG = re.compile(r"</?(?:b|i|u|br|font|span|img|p)(?:\s[^<>]*?)?\s*/?>", re.IGNORECASE)
HTML_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);")
SCENES = {"q000_wakeup_cs.w2scene", "q000_camp_choice.w2scene"}
CONSUMERS = {
    "ui_mainmenu.swf": {"Font_Style_Standard"},
    "ui_dialog.swf": {"Font_Style_Standard"},
    "ui_journal.swf": {"Font_Style_Standard", "Font_Style_Black"},
    "hud.swf": {"Font_Style_Standard", "Font_Style_Black"},
}
MARKUP = re.compile(r"<[^<>]*>|\[(?:PL|ph)\]", re.IGNORECASE)
PROBE_JOURNAL = "font-prototype.json"


def file_path(root, name, *, fallback=False):
    path = root
    parts = PurePosixPath(name).parts
    for part in parts[:-1]:
        path /= part
        if not checked_path(path, is_directory=True):
            if fallback:
                return root.joinpath(*parts)
            raise StateError(f"Missing directory: {path}")
    path /= parts[-1]
    if checked_path(path) and path.stat().st_nlink != 1:
        raise StateError(f"Hard-linked resources are not supported: {path}")
    return path


def profile_files(primary):
    stem, filename, _ = PROFILES[primary]
    return stem + ".w2strings", (GUI / filename).as_posix()


def installed_game(game):
    game = Path(game).resolve(strict=True)
    if not checked_path(game / "CookedPC", is_directory=True):
        raise StateError(f"Select the game folder containing CookedPC: {game}")
    return game


def import_fonts(raw):
    def read_string(data, pos):
        end = data.index(0, pos)
        return data[pos:end].decode("ascii"), end + 1

    imports = {}
    for tag in read_movie(raw, consumer=True).tags:
        if tag.kind not in (57, 71):
            continue
        module, pos = read_string(tag.data, 0)
        if module.lower() != "gfxfontlib.swf":
            continue
        pos += 2 if tag.kind == 71 else 0
        count, = struct.unpack_from("<H", tag.data, pos)
        pos += 2
        for _ in range(count):
            ident, = struct.unpack_from("<H", tag.data, pos)
            name, pos = read_string(tag.data, pos + 2)
            if not ident or name in imports or ident in imports.values():
                raise FontError("Duplicate Scaleform virtual font alias or import FontID.")
            imports[name] = ident
        if pos != len(tag.data):
            raise FontError("Malformed gfxfontlib import.")
    return imports


def resolved_profile(cooked, extracted, primary):
    _, name, column = PROFILES[primary]
    dependencies = {}
    def resource(relative):
        candidate = file_path(cooked, relative, fallback=True)
        loose = checked_path(candidate)
        data = read_file(candidate if loose else extracted / relative)
        dependencies[relative] = digest(data) if loose else None
        return data

    csv_bytes = resource((GUI / "fonts/fonts.csv").as_posix())
    text = csv_bytes.decode("utf-16") if csv_bytes.startswith((b"\xff\xfe", b"\xfe\xff")) else csv_bytes.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    aliases = {row["FontAlias"]: row[column].strip() for row in rows}
    if len(aliases) != len(rows) or not all(aliases.values()):
        raise FontError("Duplicate or missing fonts.csv aliases.")

    consumers = {}
    for filename, expected in CONSUMERS.items():
        resolved = import_fonts(resource((GUI / filename).as_posix()))
        if set(resolved) != expected or not expected <= aliases.keys():
            raise FontError(f"Unsupported font imports in {filename}.")
        consumers[filename] = resolved
    return resource((GUI / name).as_posix()), set(aliases.values()), aliases, consumers, dependencies


def scene_ids(data):
    if data[:8] != b"CR2Ws\0\0\0":
        raise FontError("Only CR2W v115 scene selection was verified.")
    offset, count = struct.unpack_from("<II", data, 12)
    if not 44 <= offset < len(data) or count > len(data) - offset:
        raise FontError("Invalid scene name table.")
    reader = Reader(data)
    reader.pos = offset
    names = [None]
    for _ in range(count):
        n = reader.signed()
        names.append(reader.read(-n).decode("cp1252", "replace") if n < 0
                     else reader.read(n * 2).decode("utf-16le", "replace"))
    if "LocalizedString" not in names:
        raise FontError("Scene has no typed localizations.")
    result = {}
    for field in ("dialogLine", "choiceLine"):
        if field in names:
            pattern = struct.pack("<HHHI", names.index(field), names.index("LocalizedString"), 0xffff, 8)
            result[field] = sorted({
                struct.unpack_from("<I", data, reader.pos + m.end())[0]
                for m in re.finditer(re.escape(pattern), data[reader.pos:])
                if reader.pos + m.end() + 4 <= len(data)
            })
    if not any(result.values()):
        raise FontError("No dialog/choice IDs found in scene.")
    return result


def read_catalog(path):
    raw = json.loads(read_file(path))
    if (raw["schema"], raw["units_per_em"], raw["region"]) != (1, 20480, "JP"):
        raise FontError("This test requires the Japanese donor catalog.")
    result = {}
    for code, (shape, advance, bounds) in raw["glyphs"].items():
        cp = int(code)
        shape, bounds = bytes.fromhex(shape), bytes.fromhex(bounds)
        rect = Bits(bounds)
        rect.rect()
        if (cp in result or not 0 <= cp <= 0xFFFF or not 0 < advance <= 32767
                or rect.pos != len(bounds) * 8 or not has_outline(shape)):
            raise FontError("Invalid donor glyph.")
        result[cp] = Glyph(shape, advance, bounds)
    return result


def load_sources(cooked, pair, gibbed, work):
    stems = [PROFILES[lang][0] for lang in pair]
    data = [read_file(file_path(cooked, stem + ".w2strings")) for stem in stems]
    left, right = [decode_source(raw, stem, gibbed, work) for raw, stem in zip(data, stems)]
    if left.marked_ids or right.marked_ids:
        raise StateError("Both original localizations must be free of existing dual subtitles.")
    return stems, data, left, right


def select_records(cooked, extracted, pair, gibbed, work):
    stems, data, left, right = load_sources(cooked, pair, gibbed, work)
    ids, scenes = set(), {}
    for name in sorted(SCENES):
        matches = list(extracted.rglob(name))
        if len(matches) != 1:
            raise FontError(f"Expected one scene named {name}.")
        fields = scene_ids(read_file(matches[0]))
        scenes[name] = fields
        ids.update(i for group in fields.values() for i in group)
    for key in ("menunew",):
        left_id, right_id = left.table.keys.get(key), right.table.keys.get(key)
        if left_id is None or left_id != right_id:
            raise FontError(f"Unsupported menu key mapping: {key}")
        ids.add(left_id)

    plan = plan_merge(left, right, left)
    changed = {i: plan.changes[i] for i in sorted(ids & plan.changes.keys())}
    if not changed or not ids.issuperset(changed) or not any(set(fields.get("dialogLine", [])) & changed.keys()
                                                            for fields in scenes.values()):
        raise FontError("No bilingual control dialogues could be resolved.")
    needed = set.union(*(visible_codes(MARKUP.sub("", text)) for text in changed.values()))
    if len(needed) > 512:
        raise FontError("The control set exceeds the 512-character prototype limit.")
    return stems, data, left, changed, needed, scenes, sorted(ids - changed.keys())


def missing_codes(font, needed):
    faces, _ = library_faces(read_movie(font))
    return {cp for _, face in faces.values() for cp in needed
            if cp not in face.glyphs or not has_outline(face.glyphs[cp].shape)}


def prepare_control(cooked, extracted, pair, gibbed, work, donors):
    _, font_name = profile_files(pair[0])
    font_path = file_path(cooked, font_name)
    font_before = digest(read_file(font_path)) if checked_path(font_path) else None
    stems, originals, left, changes, needed, scenes, skipped = select_records(cooked, extracted, pair, gibbed, work)
    font, names, aliases, consumers, dependencies = resolved_profile(cooked, extracted, pair[0])
    if font_before is not None and digest(font) != font_before:
        raise StateError("The loose font changed while resolving the profile.")
    swf, face_report = patch_library(font, names, needed, donors)
    if not any(face["added"] or face["filled"] for face in face_report):
        swf = font
    output, _, _ = assemble_changes(left.table, changes, stems[0], gibbed, work)
    verified = inspect_table(output, stems[0])
    if any(verified.records[i].encoded != record.encoded for i, record in left.table.records.items() if i not in changes):
        raise FontError("An unrelated localization record changed.")
    guards = {stem + ".w2strings": digest(data) for stem, data in zip(stems, originals)}
    guards.update(dependencies)
    guards[font_name] = font_before
    report = {"primary_text_language": pair[0], "secondary": pair[1], "changed_ids": sorted(changes),
              "skipped_control_ids": skipped, "control_scenes": scenes, "required_codes": sorted(needed),
              "aliases": aliases, "consumers": consumers, "faces": face_report}
    return dict(zip(profile_files(pair[0]), (output, swf))), guards, report


def record_codes(text):
    text = HTML_TAG.sub("", text)
    # Keep literal placeholder characters. Also cover characters produced if a
    # recognized HTML entity is decoded by a Scaleform consumer.
    points = set(map(ord, text))
    for match in HTML_ENTITY.finditer(text):
        points.update(map(ord, unescape(match.group())))
    return {cp for cp in points if unicodedata.category(chr(cp))[0] != "Z"}


def nonrendering(cp):
    return cp in (9, 10, 13) or unicodedata.category(chr(cp)) == "Cf" and cp <= 0xFFFF


def font_issue(cp):
    if cp > 0xFFFF:
        return "outside_DefineFont3_BMP"
    if 0xFE00 <= cp <= 0xFE0F or 0x180B <= cp <= 0x180F:
        return "variation_selector_requires_shaping"
    category = unicodedata.category(chr(cp))
    if category in ("Cn", "Cc", "Cs"):
        return "unsupported_unicode_" + category
    return None


def face_coverage(face, required):
    missing, empty = set(), set()
    for cp in required:
        glyph = face.glyphs.get(cp)
        if glyph is None:
            missing.add(cp)
        elif not has_outline(glyph.shape):
            empty.add(cp)
    return missing, empty


def donor_comparison(stock, fallback, used):
    common = sorted(used & stock.keys() & fallback.keys())
    sample = {0x0105, 0x0142, 0x017c, 0x30cb, 0x30e5, 0x30fc, 0x30b2, 0x30e0, 0x65b0, 0x898f}
    details = {}
    for cp in sorted(sample & set(common)):
        details[cp] = [
            {"advance": glyph.advance, "stored_bounds": Bits(glyph.bounds).rect(),
             "ink_bounds": ink_bounds(glyph.shape), "contours": len(shape_geometry(glyph.shape)), "shape_bytes": len(glyph.shape)}
            for glyph in (stock[cp], fallback[cp])
        ]
    return {"overlap": len(common),
            "different_shapes": sum(stock[cp].shape != fallback[cp].shape for cp in common),
            "different_advance": sum(stock[cp].advance != fallback[cp].advance for cp in common),
            "samples": details}


def unchanged_records(left, right, plan):
    pairs, ambiguous = reconcile_keys(left.table, right.table)
    by_reason = {reason: [] for reason in ("missing", "empty", "already", "opaque_kept", "ambiguous_kept")}
    for index in left.table.records.keys() - plan.changes.keys():
        donor_id = pairs.get(index, index)
        if index in ambiguous:
            reason = "ambiguous_kept"
        elif index in left.opaque or donor_id in right.opaque:
            reason = "opaque_kept"
        elif not left.texts[index].strip():
            reason = "empty"
        elif donor_id not in right.texts:
            reason = "missing"
        elif not right.texts[donor_id].strip():
            reason = "empty"
        else:
            reason = "already"
        by_reason[reason].append(index)
    if any(len(ids) != getattr(plan, reason) for reason, ids in by_reason.items()):
        raise FontError("P1 record accounting does not match its merge plan.")
    return {reason: sorted(ids) for reason, ids in by_reason.items()}


def is_han(cp):
    return 0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF


def improve_chinese_donors(secondary, used, stock, faces):
    from w2_zh_ink import freetype, improve

    eligible = {cp for cp in used & stock.keys() if is_han(cp)}
    if not eligible:
        return {}, {}
    chinese_codes = set()
    for text in secondary.texts.values():
        chinese_codes.update(map(ord, text))
    eligible &= chinese_codes
    if not eligible:
        return {}, {}
    metrics = [struct.unpack("<hhh", face.global_metrics) for _, face in faces.values()]
    ascent, descent = min(m[0] for m in metrics), min(m[1] for m in metrics)
    freetype()
    revised, retained = {}, {}
    for cp in sorted(eligible):
        glyph, reason = improve(stock[cp], ascent, descent)
        if reason is None:
            revised[cp] = glyph
        else:
            retained[cp] = reason
    return revised, retained


def improve_chinese_primary(faces, primary_codes):
    from w2_zh_ink import freetype, improve

    freetype()
    revised, retained = {}, {}
    for ident, (_, face) in faces.items():
        ascent, descent, _ = struct.unpack("<hhh", face.global_metrics)
        # Preserve layout of any Han involved in existing negative kerning.
        tight_pairs = {cp for first, second, delta in struct.iter_unpack("<HHh", face.kerning[2:])
                       if delta < 0 for cp in (first, second)}
        changed, kept = {}, {}
        for cp in sorted(primary_codes & face.glyphs.keys()):
            glyph = face.glyphs[cp]
            if not is_han(cp) or not has_outline(glyph.shape):
                continue
            if cp in tight_pairs or glyph.advance <= 0:
                kept[cp] = "stock spacing precludes a safe adjustment"
                continue
            adjusted, reason = improve(glyph, ascent, descent, primary=True)
            if reason is None:
                changed[cp] = adjusted
            else:
                kept[cp] = reason
        revised[ident], retained[ident] = changed, kept
    return revised, retained


def prepare_corpus(cooked, extracted, pair, gibbed, work, fallback):
    started = perf_counter()
    stems, originals, left, right = load_sources(cooked, pair, gibbed, work)
    plan = plan_merge(left, right, left)
    if not plan.changes:
        raise FontError("No eligible P1 records in the selected localizations.")
    font, names, aliases, consumers, dependencies = resolved_profile(cooked, extracted, pair[0])
    source, donor_names, donor_aliases, _, donor_dependencies = resolved_profile(cooked, extracted, pair[1])
    if any(dependencies[name] != donor_dependencies[name] for name in dependencies.keys() & donor_dependencies.keys()):
        raise StateError("A shared font dependency changed during resolution.")

    faces, _ = library_faces(read_movie(font))
    donor_faces, _ = library_faces(read_movie(source))
    if donor_names != {face.name for _, face in donor_faces.values()}:
        raise FontError("The donor faces do not match its font profile.")
    stock_face = next(face for _, face in donor_faces.values() if face.name == donor_aliases[DONOR_ALIAS])
    by_record = {index: record_codes(text) for index, text in plan.changes.items()}
    inventory = set().union(*by_record.values())
    controls = {cp for cp in inventory if nonrendering(cp)}
    required = inventory - controls
    issues = {cp: issue for cp in required if (issue := font_issue(cp))}
    coverage = {ident: face_coverage(face, required) for ident, (_, face) in faces.items()}
    missing = set().union(*(absent | empty for absent, empty in coverage.values()))
    stock = {cp: glyph for cp in missing - issues.keys()
             if (glyph := stock_face.glyphs.get(cp)) is not None and glyph.advance >= 0
             and has_outline(glyph.shape)}
    # The Japanese OFL catalog must never supply an alternate regional Han
    # outline to a pair without Japanese. Latin and neutral symbols may be shared.
    fallback = {cp: glyph for cp, glyph in fallback.items() if "JP" in pair or not is_han(cp)}
    donors = fallback | stock
    blocked = issues | {cp: "no_valid_donor" for cp in missing - donors.keys() - issues.keys()}
    skipped = {index: sorted(points & blocked.keys()) for index, points in by_record.items()
               if points & blocked.keys()}
    changes = {index: text for index, text in plan.changes.items() if index not in skipped}
    if not changes:
        raise FontError("The verified font profiles cannot cover any eligible bilingual record.")
    used = set().union(*(by_record[index] for index in changes)) - controls
    shared_han = []
    if {pair[0], pair[1]} == {"JP", "ZH"}:
        target = next(face for _, face in faces.values() if face.name == aliases[DONOR_ALIAS])
        for cp in sorted(used):
            if not is_han(cp):
                continue
            first, second = target.glyphs.get(cp), stock_face.glyphs.get(cp)
            if first and second and has_outline(first.shape) and has_outline(second.shape):
                if shape_geometry(first.shape) != shape_geometry(second.shape):
                    shared_han.append(cp)
    improved, retained = improve_chinese_donors(right, used, stock, faces) if pair[1] == "ZH" else ({}, {})
    donors.update(improved)
    primary_by_record = {index: record_codes(text) for index, text in left.texts.items()}
    primary_codes = set().union(*primary_by_record.values())
    primary_codes = {cp for cp in primary_codes if not nonrendering(cp)}
    existing, primary_retained = improve_chinese_primary(faces, primary_codes) if pair[0] == "ZH" else ({}, {})
    swf, face_report = patch_library(font, names, used, donors, existing=existing)
    imported_codes = {cp for details in face_report for cp in details["added"] + details["filled"]}
    ink = {cp: ink_bounds(donors[cp].shape) for cp in imported_codes}
    remaining_primary = set()
    for details in face_report:
        absent, empty = coverage[details["font_id"]]
        details["absent_before"] = sorted(absent)
        details["empty_before"] = sorted(empty)
        imported = set(details["added"] + details["filled"])
        details["stock_donor_codes"] = sorted(imported & stock.keys())
        details["ofl_fallback_codes"] = sorted(imported - stock.keys())
        details["zh_primary_stock_kept"] = primary_retained.get(details["font_id"], {})
        details["zh_adjusted_stock_codes"] = sorted(imported & improved.keys())
        details["zh_stock_kept_by_safety_check"] = {cp: retained[cp] for cp in sorted(imported & retained.keys())}
        face = faces[details["font_id"]][1]
        ascent, descent, leading = struct.unpack("<hhh", face.global_metrics)
        details["global_metrics"] = [ascent, descent, leading]
        outside = [cp for cp in sorted(imported) if ink[cp][2] < -ascent or ink[cp][3] > descent]
        details["ink_outside_ascent_descent"] = {cp: ink[cp] for cp in outside}
        original_absent, original_empty = face_coverage(face, primary_codes)
        details["primary_corpus_absent_before"] = sorted(original_absent)
        details["primary_corpus_empty_before"] = sorted(original_empty)
        remaining = (original_absent | original_empty) - imported
        details["primary_corpus_uncovered_after"] = sorted(remaining)
        remaining_primary.update(remaining)

    output, total, native = assemble_changes(left.table, changes, stems[0], gibbed, work)
    verified = inspect_table(output, stems[0])
    if (verified.keys != left.table.keys or verified.records.keys() != left.table.records.keys()
            or any(verified.records[i].encoded != record.encoded
                   for i, record in left.table.records.items() if i not in changes)):
        raise FontError("Unrelated localization bytes, keys or record IDs changed.")

    guards = {stem + ".w2strings": digest(data) for stem, data in zip(stems, originals)}
    guards.update(dependencies)
    guards.update(donor_dependencies)
    secondary_matches, _ = reconcile_keys(left.table, right.table)
    keys_by_id = {}
    for key, index in left.table.keys.items():
        if index in skipped:
            keys_by_id.setdefault(index, []).append(key)
    flagged = {index: sorted(points & controls) for index, points in by_record.items() if points & controls}
    report = {
        "scope": "corpus", "primary_text_language": pair[0], "secondary": pair[1],
        "primary_slot": profile_files(pair[0])[0], "primary_font": profile_files(pair[0])[1],
        "primary_records": total, "secondary_records": len(right.table.records),
        "p1_eligible": len(plan.changes), "p1_key_matched": plan.key_matched,
        "p1_unchanged_ids": unchanged_records(left, right, plan),
        "source_opaque_ids": {pair[0]: left.opaque, pair[1]: right.opaque},
        "secondary_only_ids": sorted(right.table.records.keys() - left.table.records.keys() - set(secondary_matches.values())),
        "changed_ids": sorted(changes),
        "skipped_glyph_records": [{"id": index, "secondary_id": secondary_matches.get(index, index),
                                   "keys": sorted(keys_by_id.get(index, ())), "codes": codes}
                                  for index, codes in sorted(skipped.items())],
        "uncovered_codes": {cp: {"reason": issue, "name": unicodedata.name(chr(cp), ""),
                                 "category": unicodedata.category(chr(cp))}
                            for cp, issue in sorted(blocked.items())},
        "required_codes_before_filter": sorted(required), "required_codes": sorted(used),
        "nonrendering_controls_by_id": dict(sorted(flagged.items())),
        "original_primary_corpus_uncovered_by_id": {
            index: sorted(points & remaining_primary) for index, points in sorted(primary_by_record.items())
            if points & remaining_primary
        },
        "native_encoded_records": native,
        "original_separator_ids": {pair[0]: sorted(left.marked_ids), pair[1]: sorted(right.marked_ids)},
        "shared_han_different_outlines_primary_retained": shared_han,
        "longest_merged_utf16_units": max(len(text.encode("utf-16le")) // 2 for text in changes.values()),
        "aliases": aliases, "consumers": consumers, "faces": face_report,
        "stock_donor": {"resource": profile_files(pair[1])[1], "name": stock_face.name,
                        "font_id": stock_face.ident, "global_metrics": struct.unpack("<hhh", stock_face.global_metrics)},
        "stock_vs_ofl": donor_comparison(stock, fallback, used & missing),
        "font_size": {"original_bytes": len(font), "patched_bytes": len(swf),
                      "original_swf_declared_bytes": struct.unpack_from("<I", font, 4)[0],
                      "patched_swf_declared_bytes": struct.unpack_from("<I", swf, 4)[0]},
        "localization_size": {"original_bytes": len(originals[0]), "patched_bytes": len(output)},
        "build_seconds": round(perf_counter() - started, 3),
    }
    return dict(zip(profile_files(pair[0]), (output, swf))), guards, report


def extract_resources(archive, output, unpacker, extensions):
    for extension in sorted(extensions):
        call = subprocess.run([str(unpacker), "-e", extension, str(archive), str(output)],
                              capture_output=True, timeout=180,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if call.returncode:
            detail = call.stderr.decode("utf-8", errors="replace").strip().splitlines()
            hint = detail[0].strip() if detail else f"exit {call.returncode}"
            raise StateError(f"Extraction failed for {archive.name} ({extension}): {hint}.")


def archive_stamp(cooked):
    result = {}
    for archive in cooked.iterdir():
        if archive.suffix.lower() != ".dzip":
            continue
        path = file_path(cooked, archive.name)
        info = path.stat()
        result[path.name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return result


def archive_names(path):
    with path.open("rb") as stream:
        try:
            magic, version, count, _, index = struct.unpack("<4sIIIQ", stream.read(24))
            size = os.fstat(stream.fileno()).st_size
            if (magic != b"DZIP" or version != 2 or not 32 <= index <= size
                    or size - index > 32 * 1024 * 1024 or count > (size - index) // 35):
                raise ValueError("unsupported DZIP index")
            stream.seek(index)
            data = stream.read()
            names = set()
            pos = 0
            for _ in range(count):
                length, = struct.unpack_from("<H", data, pos)
                pos += 2
                raw = data[pos:pos + length]
                if len(raw) != length or not raw.endswith(b"\0") or b"\0" in raw[:-1]:
                    raise ValueError("invalid archive name")
                name = raw[:-1].decode("utf-8").replace("\\", "/").lower()
                relative = PurePosixPath(name)
                if not name or relative.is_absolute() or ".." in relative.parts or ":" in name or name in names:
                    raise ValueError("ambiguous archive path")
                names.add(name)
                pos += length + 32
            if pos != len(data):
                raise ValueError("truncated DZIP index")
            return names
        except (UnicodeError, struct.error, ValueError) as error:
            raise StateError(f"Unable to verify resource overlays in {path.name}: {error}.") from error


def same_resource(left, right, relative):
    if left == right:
        return True
    if relative.endswith(".swf"):
        a, b = (read_movie(raw, consumer=True) for raw in (left, right))
        return (a.version, a.frame, a.tags) == (b.version, b.frame, b.tags)
    return False


def selected_aliases(raw, primary):
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    rows = list(csv.DictReader(io.StringIO(raw.decode(encoding)), delimiter=";"))
    aliases = {row["FontAlias"]: row[PROFILES[primary][2]].strip() for row in rows}
    if len(rows) != len(aliases) or not all(aliases.values()):
        raise FontError("Duplicate or missing fonts.csv aliases.")
    return aliases


def verify_overlays(cooked, extracted, pair, unpacker, work, stamps, *, control=False):
    consumers = {(GUI / name).as_posix(): aliases for name, aliases in CONSUMERS.items()}
    # Verify both stock font libraries. One can have a loose resource from an old test.
    fonts = {profile_files(lang)[1] for lang in pair}
    alias_file = (GUI / "fonts/fonts.csv").as_posix()
    cfonts = (GUI / "cfonts.swf").as_posix()
    relevant = fonts | consumers.keys() | {alias_file, cfonts}
    originals = {name: [read_file(extracted / name)] for name in relevant if (extracted / name).is_file()}
    controls = {path.relative_to(extracted).as_posix().lower()
                for scene in (SCENES if control else ()) for path in extracted.rglob(scene)}
    text_slots = {profile_files(lang)[0] for lang in pair}
    watched = {}

    for archive in sorted(stamps, key=str.lower):
        if archive.lower() == "pack0.dzip":
            continue
        path = file_path(cooked, archive)
        names = archive_names(path)
        if any(PurePosixPath(name).name in text_slots | (SCENES if control else set()) for name in names):
            raise StateError(f"{archive} contains another copy of the selected localization or control scene.")
        overlap = relevant & names
        if not overlap:
            continue
        overlay = work / archive
        extract_resources(path, overlay, unpacker, {PurePosixPath(name).suffix for name in overlap})
        # Gibbed preserves game filename case on a case-insensitive Windows file system.
        for name in sorted(overlap):
            data = read_file(overlay / name)
            base = originals.get(name, [])
            if name in consumers:
                valid = set(import_fonts(data)) == consumers[name]
            elif name == alias_file:
                valid = bool(base and all(selected_aliases(data, lang) == selected_aliases(base[0], lang)
                                          for lang in (pair[:1] if control else pair)))
            else:
                valid = any(same_resource(data, candidate, name) for candidate in base)
            if not valid:
                raise StateError(f"Conflicting resource {name} in {archive}.")
            originals.setdefault(name, []).append(data)

    for name in sorted(relevant | controls):
        loose = file_path(cooked, name, fallback=True)
        if not checked_path(loose):
            watched[name] = None
            continue
        data = read_file(loose)
        base = originals.get(name, []) if name not in controls else [read_file(extracted / name)]
        if not any(same_resource(data, candidate, name) for candidate in base):
            raise StateError(f"Unknown loose override: {name}. Resolve the existing mod before P2.")
        watched[name] = digest(data)
    return watched


def ensure_game_closed():
    class ProcessEntry(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("size", "usage", "pid")] + [
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD), ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG), ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot
    snapshot.argtypes, snapshot.restype = (wintypes.DWORD, wintypes.DWORD), wintypes.HANDLE
    first, next_process = kernel.Process32FirstW, kernel.Process32NextW
    for function in (first, next_process):
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        function.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL

    handle = snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise StateError("Cannot check whether The Witcher 2 is running.")
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = first(handle, ctypes.byref(entry))
        while found:
            if entry.name.casefold() == "witcher2.exe":
                raise StateError("Close The Witcher 2 before Apply or Restore.")
            found = next_process(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise StateError("Cannot check whether The Witcher 2 is running.")
    finally:
        close(handle)


class FontTransaction:
    def __init__(self, state):
        self.state = state
        self.journal = state.directory / PROBE_JOURNAL

    def save(self, journal):
        with self.state.staged(json.dumps(journal, indent=2).encode("utf-8")) as temp:
            os.replace(temp, self.journal)

    def load(self):
        if not checked_path(self.journal):
            return None
        raw = json.loads(read_file(self.journal))
        pair = tuple(raw["pair"])
        if (type(raw["schema"]) is not int or raw["schema"] != 1 or pair not in PAIRS
                or raw["status"] not in ("applying", "active", "restoring")
                or not raw["files"] or not set(raw["files"]) <= set(profile_files(pair[0]))):
            raise StateError("Invalid font test journal.")
        from w2_install import is_hash
        for name, entry in raw["files"].items():
            if not is_hash(entry["after"]) or (entry["before"] is None and name.endswith(".w2strings")):
                raise StateError("Invalid resource digest in font test journal.")
            if entry["before"] is not None and not is_hash(entry["before"]):
                raise StateError("Invalid original digest in font test journal.")
        return raw

    def current(self, name):
        path = file_path(self.state.cooked, name, fallback=True)
        return digest(read_file(path)) if checked_path(path) else None

    def switch(self, name, expected, target):
        if expected == target:
            return
        path = file_path(self.state.cooked, name)
        data = self.state.get_object(target) if target is not None else None
        if self.current(name) != expected:
            raise StateError(f"External change to {name}.")
        if data is None:
            path.unlink()
        else:
            with self.state.staged(data) as temporary:
                if self.current(name) != expected:
                    raise StateError(f"External change to {name}.")
                if expected is None:
                    os.rename(temporary, path)  # Windows rename fails if another file appeared
                else:
                    os.replace(temporary, path)
        if self.current(name) != target:
            raise StateError(f"Post-write verification failed for {name}.")

    def no_regular_install(self):
        state = self.state.state
        if state["pending"] or any(slot["result"] is not None for slot in state["slots"].values()):
            raise StateError("An active or interrupted P0/P1 install exists. Restore it before installing.")

    def preflight(self, pair, *, allow_new=False):
        if self.load() is not None:
            raise StateError("A P2/P3 install or recovery is active. Restore it first.")
        self.no_regular_install()
        legacy = self.state.cooked / LEGACY_DIR
        found_legacy = checked_path(legacy, is_directory=True)
        new = {}
        for lang in pair:
            name, _ = profile_files(lang)
            current = self.current(name)
            if current is None:
                raise StateError(f"Missing localization: {name}. Install this language in the game first.")
            record = self.state.state["slots"].get(name)
            if not record:
                if not allow_new:
                    raise StateError(f"No verified P0/P1 original for {name}. Confirm clean source files before installing.")
                new[name] = current
            else:
                self.state.get_object(record["original"])
                if current != record["original"]:
                    if not allow_new:
                        raise StateError(f"{name} differs from its recorded original.")
                    new[name] = current
            backup = legacy / (name + ".original")
            if found_legacy and checked_path(backup) and digest(read_file(backup)) != current:
                raise StateError(f"Conflicting legacy original for {name}.")
        manifest = legacy / "_cjk_assets.json"
        if found_legacy and checked_path(manifest):
            records = json.loads(read_file(manifest))
            permitted = {"globals/gui/" + path for path in
                         ("cfonts.swf", "fonts.swf", "fonts_jp.swf", "fonts_zh.swf",
                          "fonts/fonts.csv", "fonts/bll55.otf", "fonts/bll75.otf")}
            if not isinstance(records, list) or not records:
                raise StateError("Invalid legacy CJK backup manifest.")
            seen = set()
            for record in records:
                name, existed = record["rel"], record["had_original"]
                if name not in permitted or name in seen or type(existed) is not bool:
                    raise StateError("Invalid legacy CJK backup manifest.")
                seen.add(name)
                original = (legacy / "cjk_assets" / PurePosixPath(name).name)
                actual = self.current(name)
                if existed and not checked_path(legacy / "cjk_assets", is_directory=True):
                    raise StateError("Missing legacy CJK originals.")
                expected = digest(read_file(original)) if existed else None
                if actual != expected:
                    raise StateError(f"Active or conflicting legacy CJK resource: {name}.")
        return new

    def restore(self):
        raw = self.load()
        if not raw:
            return False
        self.no_regular_install()
        actual = {name: self.current(name) for name in raw["files"]}
        for name, entry in raw["files"].items():
            if actual[name] not in (entry["before"], entry["after"]):
                raise StateError(f"External change to {name}; journal and resources preserved.")
            if entry["before"] is not None:
                self.state.get_object(entry["before"])
        self.save({**raw, "status": "restoring"})
        for name, entry in reversed(tuple(raw["files"].items())):
            self.switch(name, actual[name], entry["before"])
        self.journal.unlink()
        return True

    def apply(self, pair, outputs, guards):
        if pair not in PAIRS or set(outputs) != set(profile_files(pair[0])):
            raise StateError("Unsupported resources for the font test transaction.")
        self.preflight(pair)
        if any(self.current(name) != key for name, key in guards.items()):
            raise StateError("An input localization or font changed during preparation.")
        files = {}
        for name, data in outputs.items():
            path = file_path(self.state.cooked, name)
            original = read_file(path) if checked_path(path) else None
            before = digest(original) if original is not None else None
            if before != guards[name]:
                raise StateError(f"{name} changed during snapshot preparation.")
            if before != digest(data):
                files[name] = {"before": self.state.remember(original) if original is not None else None,
                               "after": self.state.remember(data)}
        if not files:
            raise StateError("No resource changes were generated.")
        raw = {"schema": 1, "pair": pair, "status": "applying", "files": files}
        self.save(raw)
        try:
            for name, entry in files.items():
                self.switch(name, entry["before"], entry["after"])
            expected = {**guards, **{name: entry["after"] for name, entry in files.items()}}
            if any(self.current(name) != key for name, key in expected.items()):
                raise StateError("A game resource changed during installation.")
            self.save({**raw, "status": "active"})
        except Exception as error:
            try:
                self.restore()
            except Exception as recovery:
                raise StateError(f"{error}; automatic rollback pending: {recovery}") from error
            raise


def run_probe(state, pair, tools, *, apply=False, control=False, coverage_report=None, approve=None):
    if pair not in (TEST_PAIRS if control else PAIRS):
        raise StateError("Unsupported language profile combination.")
    tx = FontTransaction(state)
    new = tx.preflight(pair, allow_new=approve is not None)
    stamp = archive_stamp(state.cooked)
    pack = file_path(state.cooked, "pack0.dzip")
    unpacker = tools / "Gibbed.RED.Unpack.exe"
    # Gibbed targets the legacy Windows path limit; prefer the user's short
    # standard temp directory when the inherited sandbox temp is nested.
    preferred = Path.home() / "AppData/Local/Temp"
    default = Path(tempfile.gettempdir())
    directory = preferred if preferred.is_dir() and len(str(preferred)) < len(str(default)) else default
    with tempfile.TemporaryDirectory(prefix="w2-p2-", dir=directory) as temp:
        work = Path(temp)
        extracted = work / "pack"
        archive_names(pack)
        extensions = {".swf", ".csv", ".w2scene"} if control else {".swf", ".csv"}
        extract_resources(pack, extracted, unpacker, extensions)
        dependencies = verify_overlays(state.cooked, extracted, pair, unpacker, work, stamp, control=control)
        prepare = prepare_control if control else prepare_corpus
        outputs, guards, result = prepare(state.cooked, extracted, pair, tools / "Gibbed.RED.Strings.exe",
                                          work, read_catalog(tools.parent / "resources/donor.json"))
        if (any(guards[name] != key for name, key in dependencies.items() if name in guards)
                or any(tx.current(name) != key for name, key in dependencies.items())
                or archive_stamp(state.cooked) != stamp):
            raise StateError("A game resource changed during preparation.")
        guards.update(dependencies)
        if coverage_report is not None:
            # Publish diagnostics before starting the transaction. The report
            # describes the prepared output and is independent of the journal.
            json.dump(result, coverage_report, ensure_ascii=False, indent=2)
            coverage_report.flush()
        if apply:
            if new:
                reasons = {name: ("changed since last verification" if name in state.state["slots"] else "first use")
                           for name in new}
                if not approve(reasons):
                    return None
            ensure_game_closed()
            if archive_stamp(state.cooked) != stamp:
                raise StateError("The archive set changed before installation.")
            if new:
                slots = state.state["slots"].copy()
                for name, key in new.items():
                    data = read_file(file_path(state.cooked, name))
                    if digest(data) != key or key != guards[name]:
                        raise StateError(f"{name} changed during source verification.")
                    slots[name] = {"original": state.remember(data), "result": None}
                state.save({**state.state, "slots": slots})
            tx.apply(pair, outputs, guards)
    result["applied"] = apply
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "restore"))
    parser.add_argument("--game", type=Path, required=True, help="Installed game folder")
    parser.add_argument("--pair", default="PL-JP", help="Ordered language codes, e.g. EN-ZH")
    parser.add_argument("--accept-clean-sources", action="store_true",
                        help="Confirm that new or changed localization sources are clean originals")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report_path = args.report.resolve()
    game_path = args.game.resolve()
    if game_path == report_path or game_path in report_path.parents:
        return 1
    try:
        if checked_path(report_path) and report_path.stat().st_nlink != 1:
            return 1
        report = report_path.open("w", encoding="utf-8")
    except (OSError, StateError):
        return 1
    status = 0
    with report:
        try:
            game = installed_game(game_path)
            state = GameState(game)
            tools = Path(__file__).resolve().parent / "Gibbed RED Tools"
            pair = tuple(args.pair.upper().split("-"))
            approve = (lambda _: True) if args.accept_clean_sources else None
            if args.action == "prepare":
                state.state = state.load()
                result = run_probe(state, pair, tools, approve=approve)
            else:
                with state.locked(create=args.action == "apply") as locked:
                    tx = FontTransaction(state)
                    if args.action == "restore":
                        if locked and tx.load():
                            ensure_game_closed()
                        result = {"restored": bool(locked and tx.restore())}
                    else:
                        result = run_probe(state, pair, tools, apply=True, coverage_report=report, approve=approve)
        except Exception as error:
            result, status = {"error": str(error)}, 1
        report.seek(0)
        json.dump(result, report, ensure_ascii=False, indent=2)
        report.write("\n")
        report.truncate()
    return status
