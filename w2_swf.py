"""Conservative reader/writer for Witcher 2's stand-alone DefineFont3 libraries."""
from dataclasses import dataclass, replace
from functools import lru_cache
import math
import struct
import unicodedata
import zlib


class FontError(ValueError):
    pass


class Bits:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        end = self.pos + n
        if end > len(self.data) * 8:
            raise FontError("Truncated bit field.")
        if not n:
            return 0
        raw = int.from_bytes(self.data[self.pos // 8:(end + 7) // 8], "big")
        self.pos = end
        return raw >> (-end % 8) & ((1 << n) - 1)

    def signed(self, n):
        value = self.take(n)
        return value - (1 << n) if n and value >> (n - 1) else value

    def rect(self):
        n = self.take(5)
        bounds = tuple(self.signed(n) for _ in range(4))
        if bounds[0] > bounds[1] or bounds[2] > bounds[3]:
            raise FontError("Invalid RECT bounds.")
        self.pos = (self.pos + 7) // 8 * 8
        return bounds


class BitWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.pending = 0
        self.count = 0

    def put(self, value, n):
        self.pending = (self.pending << n) | (value & ((1 << n) - 1))
        self.count += n
        while self.count >= 8:
            self.count -= 8
            self.buffer.append(self.pending >> self.count)
            self.pending &= (1 << self.count) - 1

    def finish(self):
        if self.count:
            self.put(0, 8 - self.count)
        return bytes(self.buffer)


def width(*values):
    return max((x if x >= 0 else ~x).bit_length() + 1 for x in values)


def rect_bytes(bounds):
    w = BitWriter()
    n = width(*bounds)
    if n > 31:
        raise FontError("RECT coordinates exceed the format.")
    w.put(n, 5)
    for value in bounds:
        w.put(value, n)
    return w.finish()


def shape_geometry(data, *, strict_fill=False):
    """Read SHAPE move/edge records and reject embedded style definitions."""
    reader = Bits(data)
    fill_bits, line_bits = reader.take(4), reader.take(4)
    if strict_fill and (fill_bits, line_bits) != (1, 0):
        raise FontError("unsupported donor fill or line style layout")
    fill0 = fill1 = 0
    point = (0, 0)
    contours = []
    for _ in range(len(data) * 2):
        edge = reader.take(1)
        if not edge:
            flags = reader.take(5)
            if not flags:
                if len(data) * 8 - reader.pos > 7 or reader.take(len(data) * 8 - reader.pos):
                    raise FontError("Unexpected data after font SHAPE end.")
                return contours
            if flags & 16:
                raise FontError("A font SHAPE defines extra styles.")
            if flags & 1:
                n = reader.take(5)
                point = reader.signed(n), reader.signed(n)
                contours.append([point])
            if flags & 2:
                fill0 = reader.take(fill_bits)
            if flags & 4:
                fill1 = reader.take(fill_bits)
            if flags & 8:
                reader.take(line_bits)
            continue
        if strict_fill and (fill0, fill1) != (1, 0):
            raise FontError("unsupported donor edge fill")
        straight, n = reader.take(1), reader.take(4) + 2
        if not contours:
            contours.append([point])
        if straight:
            if reader.take(1):
                dx, dy = reader.signed(n), reader.signed(n)
            elif reader.take(1):
                dx, dy = 0, reader.signed(n)
            else:
                dx, dy = reader.signed(n), 0
            end = (point[0] + dx, point[1] + dy)
            contours[-1].append((end,))
        else:
            control = point[0] + reader.signed(n), point[1] + reader.signed(n)
            end = control[0] + reader.signed(n), control[1] + reader.signed(n)
            contours[-1].append((control, end))
        point = end
    raise FontError("Unterminated font SHAPE.")


@lru_cache(maxsize=32768)
def has_outline(data):
    for contour in shape_geometry(data):
        point = contour[0]
        for segment in contour[1:]:
            if any(endpoint != point for endpoint in segment):
                return True
            point = segment[-1]
    return False


@lru_cache(maxsize=32768)
def ink_bounds(data):
    """Measure actual SHAPE bounds, including quadratic extrema."""
    axes = [[], []]
    for start, *segments in shape_geometry(data):
        if not segments:
            continue
        for segment in segments:
            end = segment[-1]
            for axis, values in enumerate(axes):
                a, b = start[axis], end[axis]
                values.extend((a, b))
                if len(segment) == 2:
                    c = segment[0][axis]
                    denominator = a - 2 * c + b
                    if denominator:
                        t = (a - c) / denominator
                        if 0 < t < 1:
                            values.append(a - (a - c) * t)
            start = end
    return tuple(bound for values in axes for bound in
                 (math.floor(min(values, default=0)), math.ceil(max(values, default=0))))


def write_shape(contours):
    writer = BitWriter()
    writer.put(0x10, 8)
    for i, contour in enumerate(contours):
        x, y = contour[0]
        writer.put(3 if i == 0 else 1, 6)  # MoveTo and FillStyle0, as in the stock faces.
        n = width(x, y)
        if n > 31:
            raise FontError("Glyph origin exceeds the SHAPE format.")
        writer.put(n, 5)
        writer.put(x, n)
        writer.put(y, n)
        if i == 0:
            writer.put(1, 1)
        for segment in contour[1:]:
            deltas = []
            for xx, yy in segment:
                deltas.extend((xx - x, yy - y))
                x, y = xx, yy
            n = max(2, width(*deltas))
            if n > 17:
                raise FontError("Glyph edge exceeds the SHAPE format.")
            straight = len(segment) == 1
            writer.put(3 if straight else 2, 2)
            writer.put(n - 2, 4)
            if straight:
                writer.put(1, 1)
            for delta in deltas:
                writer.put(delta, n)
    writer.put(0, 6)
    data = writer.finish()
    if shape_geometry(data) != contours:
        raise FontError("Font SHAPE failed point-by-point verification.")
    return data


@dataclass(frozen=True)
class Tag:
    kind: int
    data: bytes
    header: bytes


@dataclass(frozen=True)
class Movie:
    signature: bytes
    version: int
    frame: bytes
    tags: tuple
    padded: bool = False


def read_movie(data, *, consumer=False):
    if len(data) < 8 or data[:3] not in (b"FWS", b"CWS") or data[3] < 8:
        raise FontError("Expected an SWF 8+ FWS/CWS resource.")
    length = struct.unpack_from("<I", data, 4)[0] - 8
    if not 0 < length <= 96 * 1024 * 1024:
        raise FontError("SWF length is outside the supported range.")
    body = data[8:]
    padded = False
    if data[:3] == b"CWS":
        decoder = zlib.decompressobj()
        body = decoder.decompress(body, length + 1)
        if decoder.unused_data:
            padded = len(data) == length + 16 and not any(decoder.unused_data)
        if not decoder.eof or (decoder.unused_data and not padded) or len(body) != length:
            raise FontError("Invalid SWF zlib member.")
    elif consumer and len(body) == length + 8:
        body = body[:length]  # observed opaque trailer on some read-only game consumers
    if len(body) != length:
        raise FontError("SWF length mismatch.")
    bits = Bits(body)
    bits.rect()
    pos = bits.pos // 8 + 4
    frame = body[:pos]
    tags = []
    while pos < length:
        start = pos
        header, = struct.unpack_from("<H", body, pos)
        pos += 2
        kind, size = header >> 6, header & 63
        if size == 63:
            size, = struct.unpack_from("<I", body, pos)
            pos += 4
        end = pos + size
        if end > length:
            raise FontError("SWF tag exceeds the declared length.")
        tags.append(Tag(kind, body[pos:end], body[start:pos]))
        pos = end
        if kind == 0:
            if size or pos != length:
                raise FontError("Invalid SWF End tag.")
            return Movie(data[:3], data[3], frame, tuple(tags), padded)
    raise FontError("SWF End tag missing.")


def write_movie(movie):
    body = movie.frame + b"".join(tag.header + tag.data for tag in movie.tags)
    payload = zlib.compress(body, 9) if movie.signature == b"CWS" else body
    if movie.padded:
        if len(payload) > len(body):
            raise FontError("Compressed SWF no longer fits the zero-padded container.")
        payload = payload.ljust(len(body) + 8, b"\0")
    return movie.signature + bytes([movie.version]) + struct.pack("<I", 8 + len(body)) + payload


def tagged(kind, data):
    size = len(data)
    header = (struct.pack("<H", (kind << 6) | size) if size < 63
              else struct.pack("<HI", (kind << 6) | 63, size))
    return Tag(kind, data, header)


@dataclass(frozen=True)
class Glyph:
    shape: bytes
    advance: int
    bounds: bytes


@dataclass(frozen=True)
class Face:
    ident: int
    name: str
    prefix: bytes
    global_metrics: bytes
    kerning: bytes
    glyphs: dict


def read_face(data):
    ident, flags, _, name_size = struct.unpack_from("<HBBB", data)
    if not ident or flags & 0xC4 != 0x84:
        raise FontError("DefineFont3 requires Unicode codes and layout, without Shift-JIS.")
    start = 7 + name_size
    name = data[5:5 + name_size].rstrip(b"\0").decode("utf-8")
    count, = struct.unpack_from("<H", data, start - 2)
    if not name or not count:
        raise FontError("Missing font name or glyphs.")
    fmt = "I" if flags & 8 else "H"
    offsets = struct.unpack_from(f"<{count + 1}{fmt}", data, start)
    if (offsets[0] != struct.calcsize(f"<{count + 1}{fmt}")
            or any(a >= b for a, b in zip(offsets, offsets[1:]))):
        raise FontError("Invalid glyph offsets.")
    pos = start + offsets[-1]
    codes = struct.unpack_from(f"<{count}H", data, pos)
    if codes != tuple(sorted(set(codes))):
        raise FontError("Unicode table must be sorted and unique.")
    pos += 2 * count
    metrics = data[pos:pos + 6]
    pos += 6
    advances = struct.unpack_from(f"<{count}h", data, pos)
    pos += count * 2
    glyphs = {}
    for index, cp in enumerate(codes):
        bits = Bits(data[pos:pos + 17])
        bits.rect()
        end = pos + bits.pos // 8
        glyphs[cp] = Glyph(data[start + offsets[index]:start + offsets[index + 1]],
                           advances[index], data[pos:end])
        pos = end
    kern_count, = struct.unpack_from("<H", data, pos)
    if pos + 2 + 6 * kern_count != len(data):
        raise FontError("DefineFont3 kerning table or tag length is inconsistent.")
    return Face(ident, name, data[:start - 2], metrics, data[pos:], glyphs)


def write_face(face):
    items = sorted(face.glyphs.items())
    if len(items) > 65535:
        raise FontError("DefineFont3 glyph limit exceeded.")
    prefix = bytearray(face.prefix)
    wide = bool(prefix[2] & 8)
    shape_bytes = sum(len(glyph.shape) for _, glyph in items)
    if (len(items) + 1) * 2 + shape_bytes > 65535:
        wide = True
        prefix[2] |= 8
    fmt = "I" if wide else "H"
    offset = (len(items) + 1) * struct.calcsize(fmt)
    offsets = [offset]
    for _, glyph in items:
        offset += len(glyph.shape)
        offsets.append(offset)
    return (bytes(prefix) + struct.pack("<H", len(items))
            + struct.pack(f"<{len(offsets)}{fmt}", *offsets)
            + b"".join(g.shape for _, g in items)
            + struct.pack(f"<{len(items)}H", *(code for code, _ in items))
            + face.global_metrics + struct.pack(f"<{len(items)}h", *(g.advance for _, g in items))
            + b"".join(g.bounds for _, g in items) + face.kerning)


def read_zones(data, face):
    if len(data) != 3 + 10 * len(face.glyphs) or data[2] & 0x3F:
        raise FontError("Unsupported DefineFontAlignZones layout.")
    zones = {}
    for i, code in enumerate(face.glyphs):
        chunk = data[3 + i * 10:13 + i * 10]
        if chunk[0] != 2 or chunk[-1] & 0xFC:
            raise FontError("Unsupported alignment zone record.")
        zones[code] = chunk
    return zones


def dynamic_font(data):
    bits = Bits(data)
    bits.take(16)
    bits.rect()
    flags = bits.take(16)
    return int.from_bytes(data[bits.pos // 8:bits.pos // 8 + 2], "little") if flags & 0x0100 else None


def library_faces(movie):
    # Font-only libraries can use DefineEditText, which refers to a FontID.
    # DefineText and unknown/nested definitions may refer to glyph indices.
    allowed = {0, 1, 9, 26, 37, 69, 73, 74, 75, 88}
    if any(tag.kind not in allowed for tag in movie.tags):
        raise FontError("Unverified library tag; possible glyph-index or nested dependency.")
    faces, zones = {}, {}
    for i, tag in enumerate(movie.tags):
        if tag.kind == 75:
            face = read_face(tag.data)
            if face.ident in faces or any(old.name == face.name for _, old in faces.values()):
                raise FontError("Duplicate font ID or name.")
            faces[face.ident] = (i, face)
        elif tag.kind == 73:
            ident, = struct.unpack_from("<H", tag.data)
            if ident not in faces or ident in zones:
                raise FontError("Unresolved or duplicate alignment zone FontID.")
            zones[ident] = i, read_zones(tag.data, faces[ident][1])
    font_references = {dynamic_font(tag.data) for tag in movie.tags if tag.kind == 37}
    if not faces or font_references - set(faces) - {None} or not set(faces) <= font_references:
        raise FontError("Dynamic text does not resolve the expected FontIDs.")
    for tag in movie.tags:
        if tag.kind == 88 and struct.unpack_from("<H", tag.data)[0] not in faces:
            raise FontError("Font name record refers to an unresolved FontID.")
    return faces, zones


def visible_codes(text):
    # Strip recognized markup separately in the caller. Spaces and nonprinting
    # controls have no visible outlines; combining marks are retained.
    return {ord(c) for c in text if unicodedata.category(c)[0] not in ("C", "Z")}


def patch_library(data, names, required, donors, *, existing=None):
    if any(cp > 0xFFFF or 0xD800 <= cp <= 0xDFFF for cp in required):
        raise FontError("The stock DefineFont3 Unicode table supports only BMP code points.")
    movie = read_movie(data)
    faces, zones = library_faces(movie)
    if set(names) != {face.name for _, face in faces.values()}:
        raise FontError("Font faces do not match the resolved primary profile.")
    existing = existing or {}
    if existing.keys() - faces.keys():
        raise FontError("An outline replacement refers to an unknown FontID.")
    tags, results, expected = list(movie.tags), [], {}
    for ident, (index, face) in faces.items():
        glyphs = face.glyphs.copy()
        added, filled = [], []
        for cp in sorted(required):
            old = glyphs.get(cp)
            if old is not None and has_outline(old.shape):
                continue
            donor = donors.get(cp)
            if donor is None or not has_outline(donor.shape):
                raise FontError(f"No licensed, nonempty donor for U+{cp:04X} in {face.name}.")
            glyphs[cp] = donor
            (filled if old is not None else added).append(cp)
        corrected = existing.get(ident, {})
        for cp, glyph in corrected.items():
            original = face.glyphs.get(cp)
            if (original is None or not has_outline(original.shape)
                    or not has_outline(glyph.shape) or glyph.advance != original.advance):
                raise FontError("A Primary outline replacement would change its glyph mapping or advance.")
            glyphs[cp] = glyph
        updated = replace(face, glyphs=glyphs)
        if added or filled or corrected:
            tags[index] = tagged(75, write_face(updated))
            if ident in zones:
                changed_codes = set(added + filled) | corrected.keys()
                zone_index, records = zones[ident]
                neutral = b"\x02" + b"\0" * 9
                zone = movie.tags[zone_index].data[:3] + b"".join(
                    neutral if cp in changed_codes else records[cp] for cp in sorted(glyphs)
                )
                tags[zone_index] = tagged(73, zone)
        expected[ident] = glyphs
        results.append({"font_id": ident, "name": face.name, "original": len(face.glyphs),
                        "preserved": len(face.glyphs) - len(filled) - len(corrected),
                        "added": added, "filled": filled, "adjusted_existing": sorted(corrected)})
    if not any(row["added"] or row["filled"] or row["adjusted_existing"] for row in results):
        return data, results
    output = write_movie(replace(movie, tags=tuple(tags)))
    verified = read_movie(output)
    new_faces, new_zones = library_faces(verified)
    if verified.frame != movie.frame or verified.tags != tuple(tags):
        raise FontError("SWF container verification failed.")
    for ident, (_, old) in faces.items():
        new = new_faces[ident][1]
        if (new.name, new.global_metrics, new.kerning, new.glyphs) != (
                old.name, old.global_metrics, old.kerning, expected[ident]):
            raise FontError("Font ID, metrics or glyph integrity verification failed.")
        if ident in zones:
            for cp, record in zones[ident][1].items():
                if old.glyphs[cp] == new.glyphs[cp] and new_zones[ident][1][cp] != record:
                    raise FontError("An original alignment zone changed.")
    return output, results

