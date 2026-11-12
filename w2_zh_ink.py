"""Bounded geometric correction for Traditional Chinese outlines."""
import ctypes as C
from functools import lru_cache
from pathlib import Path

from w2_swf import FontError, Glyph, ink_bounds, rect_bytes, shape_geometry, write_shape


# DefineFont3 uses a 20480-unit em. Advance stays in the stock coordinate grid.
LEVELS = ((320, 520, 108), (220, 360, 105), (120, 220, 103))


class Vector(C.Structure):
    _fields_ = [("x", C.c_long), ("y", C.c_long)]


class Outline(C.Structure):
    _fields_ = [
        ("n_contours", C.c_ushort), ("n_points", C.c_ushort),
        ("points", C.POINTER(Vector)), ("tags", C.POINTER(C.c_ubyte)),
        ("contours", C.POINTER(C.c_ushort)), ("flags", C.c_int),
    ]


@lru_cache(maxsize=1)
def freetype():
    location = Path(__file__).resolve().parent / "resources/freetype.dll"
    try:
        library = C.CDLL(str(location))
        library.FT_Outline_Check.argtypes = [C.POINTER(Outline)]
        library.FT_Outline_Check.restype = C.c_int
        library.FT_Outline_EmboldenXY.argtypes = [C.POINTER(Outline), C.c_long, C.c_long]
        library.FT_Outline_EmboldenXY.restype = C.c_int
        return library
    except (OSError, AttributeError) as error:
        raise FontError("The bundled FreeType outline library is unavailable. Extract the complete package.") from error


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def contour_area(contour):
    """Six times the signed area, including quadratic edges."""
    previous = contour[0]
    area = 0
    for segment in contour[1:]:
        end = segment[-1]
        if len(segment) == 2:
            control = segment[0]
            area += 2 * (cross(previous, control) + cross(control, end)) + cross(previous, end)
        else:
            area += 3 * cross(previous, end)
        previous = end
    return area


def transform(contours, function):
    return [[function(contour[0]), *(tuple(function(p) for p in edge) for edge in contour[1:])]
            for contour in contours]


def embolden(contours, strength_x, strength_y):
    points, tags, ends = [], [], []
    for contour in contours:
        if len(contour) < 2 or contour[0] != contour[-1][-1]:
            raise FontError("open or empty donor contour")
        points.append(contour[0])
        tags.append(1)
        for segment in contour[1:]:
            if len(segment) == 2:
                points.append(segment[0])
                tags.append(0)
            points.append(segment[-1])
            tags.append(1)
        points.pop()
        tags.pop()
        ends.append(len(points) - 1)
    if len(points) > 32767 or len(contours) > 32767:
        raise FontError("FreeType outline limits exceeded")
    if any(abs(x) > 65535 or abs(y) > 65535 for x, y in points):
        raise FontError("donor coordinates exceed the safe outline range")
    # FreeType operates with a positive-up Y axis.
    vectors = (Vector * len(points))(*(Vector(x, -y) for x, y in points))
    tag_array = (C.c_ubyte * len(tags))(*tags)
    end_array = (C.c_ushort * len(ends))(*ends)
    outline = Outline(len(ends), len(points), vectors, tag_array, end_array, 0)
    library = freetype()
    if library.FT_Outline_Check(C.byref(outline)):
        raise FontError("invalid FreeType outline")
    if library.FT_Outline_EmboldenXY(C.byref(outline), strength_x, strength_y):
        raise FontError("FreeType could not embolden the outline")
    result = []
    cursor = 0
    def point(index):
        return vectors[index].x - strength_x // 2, -vectors[index].y + strength_y // 2
    for contour, last in zip(contours, ends):
        start = point(cursor)
        revised = [start]
        cursor += 1
        for segment in contour[1:]:
            edge = []
            for _ in segment:
                edge.append(start if cursor > last else point(cursor))
                cursor += 1
            revised.append(tuple(edge))
        cursor -= 1  # the implicit closing point belongs to the next contour
        result.append(revised)
    return result


PRIMARY_SIDE_GAP = 160


def primary_side_limits(glyph):
    left, right, _, _ = ink_bounds(glyph.shape)
    return min(left, PRIMARY_SIDE_GAP), max(right, glyph.advance - PRIMARY_SIDE_GAP)


def fit_primary_width(contours, glyph, left, right):
    low, high = primary_side_limits(glyph)
    if low <= left and right <= high:
        return contours
    if left == right or high <= low:
        raise FontError("invalid Primary glyph cell")
    scale = min(1, (high - low - 2) / (right - left))
    if scale < 0.96:
        raise FontError("enlarged outline exceeds the Primary horizontal cell")
    half = (right - left) * scale / 2
    center = max(low + half, min((left + right) / 2, high - half))
    return transform(contours, lambda p: (round(center + (p[0] - (left + right) / 2) * scale), p[1]))


def fit_outline(original, glyph, ascent, descent, strength_x, strength_y, height, primary):
    modified = embolden(original, strength_x, strength_y)
    _, _, top, bottom = ink_bounds(write_shape(modified))
    if top == bottom:
        raise FontError("zero-height emboldened outline")
    scale = min(height / 100, (ascent + descent - 2) / (bottom - top))
    if scale < 0.98:
        raise FontError("enlarged outline exceeds the Primary line metrics")
    tall = transform(modified, lambda p: (p[0], round(p[1] * scale)))
    shape = write_shape(tall)
    x_min, x_max, y_min, y_max = ink_bounds(shape)
    # Fit the contour vertically only when its ink exceeds the line cell.
    offset = max(-ascent - y_min, min(0, descent - y_max))
    if offset:
        tall = transform(tall, lambda p: (p[0], p[1] + offset))
        shape = write_shape(tall)
    if primary:
        tall = fit_primary_width(tall, glyph, x_min, x_max)
        shape = write_shape(tall)
    bounds = ink_bounds(shape)
    if primary:
        low, high = primary_side_limits(glyph)
        if bounds[0] < low or bounds[1] > high:
            raise FontError("enlarged outline could not fit the Primary horizontal cell")
    old_bounds = ink_bounds(glyph.shape)
    if bounds[2] < -ascent or bounds[3] > descent:
        raise FontError("enlarged outline could not fit the Primary line metrics")
    if x_min < min(0, old_bounds[0]) - strength_x or x_max > max(glyph.advance, old_bounds[1]) + strength_x:
        raise FontError("enlarged outline would overhang the stock advance")
    before = [contour_area(contour) for contour in original]
    after = [contour_area(contour) for contour in tall]
    if any(old * new <= 0 or abs(new) * 2 < abs(old) for old, new in zip(before, after)):
        raise FontError("a contour would collapse or change winding")
    old_ink, new_ink = abs(sum(before)), abs(sum(after))
    if not old_ink < new_ink <= old_ink * 2:
        raise FontError("net ink area increase falls outside the permitted range")
    return Glyph(shape, glyph.advance, rect_bytes(bounds))


@lru_cache(maxsize=8192)
def improve(glyph, ascent, descent, *, primary=False):
    """Return the stock glyph with a reason when no correction can be verified."""
    try:
        original = shape_geometry(glyph.shape, strict_fill=True)
    except FontError as error:
        return glyph, str(error)
    for level in LEVELS:
        try:
            return fit_outline(original, glyph, ascent, descent, *level, primary), None
        except FontError as error:
            reason = str(error)
    return glyph, reason
