"""Build a redistributable glyph catalog from a local licensed TrueType donor."""
import argparse
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import json
from pathlib import Path
import struct

from w2_swf import Glyph, FontError, rect_bytes, write_shape


class GlyphMetrics(C.Structure):
    _fields_ = [("width", W.UINT), ("height", W.UINT), ("origin", W.POINT),
                ("advance_x", W.SHORT), ("advance_y", W.SHORT)]


class Fixed(C.Structure):
    _fields_ = [("fraction", W.WORD), ("value", W.SHORT)]


class Matrix(C.Structure):
    _fields_ = [(name, Fixed) for name in ("m11", "m12", "m21", "m22")]


def gdi_function(name, result, *args):
    function = getattr(C.windll.gdi32, name)
    function.restype, function.argtypes = result, args
    return function


@contextmanager
def selected_font(data, family, weight):
    ptr = C.create_string_buffer(data)
    count = W.DWORD()
    add = gdi_function("AddFontMemResourceEx", W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD))
    remove = gdi_function("RemoveFontMemResourceEx", W.BOOL, W.HANDLE)
    create_dc = gdi_function("CreateCompatibleDC", W.HDC, W.HDC)
    delete_dc = gdi_function("DeleteDC", W.BOOL, W.HDC)
    create = gdi_function("CreateFontW", W.HFONT, *([C.c_int] * 5), *([W.DWORD] * 8), W.LPCWSTR)
    select = gdi_function("SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    delete = gdi_function("DeleteObject", W.BOOL, W.HGDIOBJ)
    font_data = gdi_function("GetFontData", W.DWORD, W.HDC, W.DWORD, W.DWORD, C.c_void_p, W.DWORD)
    handle = add(ptr, len(data), None, C.byref(count))
    if not handle:
        raise FontError("Windows could not load the donor in memory.")
    dc = font = previous = None
    try:
        dc = create_dc(None)
        # 2048 device units per em, scaled by ten to the DefineFont3 em.
        font = create(-2048, 0, 0, 0, weight, 0, 0, 0, 1, 7, 0, 0, 0, family)
        if not dc or not font:
            raise FontError("Windows could not create the donor font context.")
        previous = select(dc, font)
        size = font_data(dc, 0, 0, None, 0)
        if size != len(data):
            raise FontError("Windows selected a different font from the licensed donor.")
        actual = C.create_string_buffer(size)
        if font_data(dc, 0, 0, actual, size) != size or actual.raw != data:
            raise FontError("Selected font bytes do not match the donor.")
        yield dc
    finally:
        if dc and previous:
            select(dc, previous)
        if font:
            delete(font)
        if dc:
            delete_dc(dc)
        remove(handle)


def point(raw, offset):
    x, y = struct.unpack_from("<ii", raw, offset)
    return (x * 10 / 65536, -y * 10 / 65536)


def rounded(value):
    return tuple(round(v) for v in value)


def contours_from_native(data):
    contours = []
    pos = 0
    while pos < len(data):
        length, kind = struct.unpack_from("<II", data, pos)
        end = pos + length
        if kind != 24 or length < 16 or end > len(data):
            raise FontError("Unexpected native contour header.")
        start = point(data, pos + 8)
        contour = [rounded(start)]
        pos += 16
        while pos < end:
            curve, count = struct.unpack_from("<HH", data, pos)
            pos += 4
            if not count or pos + count * 8 > end:
                raise FontError("Truncated native curve.")
            points = [point(data, pos + i * 8) for i in range(count)]
            pos += count * 8
            if curve == 1:
                contour.extend((rounded(p),) for p in points)
            elif curve == 2 and count >= 2:
                for i, control in enumerate(points[:-1]):
                    nxt = points[i + 1]
                    endpoint = nxt if i == count - 2 else tuple((a + b) / 2 for a, b in zip(control, nxt))
                    contour.append((rounded(control), rounded(endpoint)))
            else:
                raise FontError("Expected line or quadratic TrueType outline.")
        if len(contour) > 1:
            if contour[-1][-1] != contour[0]:
                contour.append((contour[0],))
            # GDI returns clockwise outer contours after flipping Y. The stock
            # SWF faces use the opposite winding with FillStyle0.
            starts = [contour[0]] + [segment[-1] for segment in contour[1:]]
            reversed_segments = [segment[:-1] + (previous,)
                                 for previous, segment in reversed(list(zip(starts, contour[1:])))]
            contours.append([starts[-1], *reversed_segments])
    return contours


def collect(path, family, codes, weight=400):
    data = path.read_bytes()
    matrix = Matrix(Fixed(0, 1), Fixed(0, 0), Fixed(0, 0), Fixed(0, 1))
    outline = gdi_function("GetGlyphOutlineW", W.DWORD, W.HDC, W.UINT, W.UINT,
                           C.POINTER(GlyphMetrics), W.DWORD, C.c_void_p, C.POINTER(Matrix))
    indices = gdi_function("GetGlyphIndicesW", W.DWORD, W.HDC, W.LPCWSTR, C.c_int, C.POINTER(W.WORD), W.DWORD)
    result = {}
    with selected_font(data, family, weight) as dc:
        for cp in sorted(codes):
            if not 0 <= cp <= 0xFFFF or 0xD800 <= cp <= 0xDFFF:
                raise FontError("Only BMP Unicode scalars are supported by DefineFont3.")
            index = W.WORD()
            if indices(dc, chr(cp), 1, C.byref(index), 1) != 1 or index.value in (0, 0xFFFF):
                continue
            metrics = GlyphMetrics()
            flags = 2 | 0x80 | 0x100
            size = outline(dc, index.value, flags, C.byref(metrics), 0, None, C.byref(matrix))
            if size in (0, 0xFFFFFFFF):
                continue
            buffer = C.create_string_buffer(size)
            if outline(dc, index.value, flags, C.byref(metrics), size, buffer, C.byref(matrix)) != size:
                raise FontError(f"Native outline read failed for U+{cp:04X}.")
            contours = contours_from_native(buffer.raw)
            if not contours:
                continue
            xmin, ymax = metrics.origin.x * 10, -metrics.origin.y * 10
            bounds = xmin, xmin + metrics.width * 10, ymax, ymax + metrics.height * 10
            advance = metrics.advance_x * 10
            if not 0 < advance <= 32767:
                raise FontError(f"Invalid donor advance for U+{cp:04X}.")
            result[cp] = Glyph(write_shape(contours), advance, rect_bytes(bounds))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ttf", type=Path)
    parser.add_argument("codes", type=Path, help="JSON list of Unicode code points")
    parser.add_argument("output", type=Path)
    parser.add_argument("--latin", type=Path, help="Local OFL Noto Sans TrueType for missing Latin/Cyrillic")
    parser.add_argument("--family", default="Noto Sans CJK JP")
    args = parser.parse_args()
    codes = set(json.loads(args.codes.read_text("utf-8")))
    glyphs = collect(args.ttf, args.family, codes)
    sources = {args.family: sha256(args.ttf.read_bytes()).hexdigest()}
    if args.latin and codes - glyphs.keys():
        remaining = codes - glyphs.keys()
        if any(not (0x20 <= cp <= 0x052F or 0x1E00 <= cp <= 0x1EFF or 0x2000 <= cp <= 0x206F)
               for cp in remaining):
            raise FontError("The JP face lacks a non-Latin code; use the correct regional donor.")
        glyphs.update(collect(args.latin, "Noto Sans", remaining))
        sources["Noto Sans"] = sha256(args.latin.read_bytes()).hexdigest()
    if set(glyphs) != codes:
        raise FontError(f"Donor missing requested outlines: {sorted(codes - glyphs.keys())}")
    payload = {"schema": 1, "units_per_em": 20480, "family": args.family, "region": "JP",
               "license": "SIL Open Font License 1.1", "source_sha256": sources,
               "glyphs": {str(cp): [g.shape.hex(), g.advance, g.bounds.hex()] for cp, g in glyphs.items()}}
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(glyphs)} donor outlines.")


if __name__ == "__main__":
    main()

