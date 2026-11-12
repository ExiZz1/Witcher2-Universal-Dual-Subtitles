"""Read, validate, and rebuild Witcher 2 v201 localization tables."""
from dataclasses import dataclass
from functools import lru_cache
import re
import struct
import subprocess
import xml.etree.ElementTree as ET


SEPARATOR = " ~ "
FORMAT_VERSION = 201
# v201 locale key and XOR mask. These describe the codec/slot, never a file revision.
LOCALE_KEYS = {
    "cz0": (0x24987354, 0x21793217),
    "de0": (0x75886138, 0x42791159),
    "en0": (0x43975139, 0x79321793),
    "es0": (0x18796651, 0x42387566),
    "fr0": (0x23863176, 0x75921975),
    "hu0": (0x42378932, 0x67823218),
    "it0": (0x45931894, 0x12375973),
    "jp0": (0x54834893, 0x59825646),
    "pl0": (0x83496237, 0x73946816),
    "ru0": (0x63481486, 0x42386347),
    "tr0": (0x56432683, 0x21795135),
    "zh0": (0x18632176, 0x16875467),
}
XML_REFERENCE = re.compile(r"&#(?:x[0-9a-fA-F]+|[0-9]+);")
XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class FormatError(RuntimeError):
    pass


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, size):
        end = self.pos + size
        if size < 0 or end > len(self.data):
            raise FormatError("Truncated .w2strings record.")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def unpack(self, layout):
        return struct.unpack(layout, self.read(struct.calcsize(layout)))

    def signed(self):
        first = self.read(1)[0]
        value = first & 0x3F
        if first & 0x40:
            for shift in (6, 13, 20, 27):
                byte = self.read(1)[0]
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
            else:
                raise FormatError("Oversized encoded integer.")
        if value > 0x7FFFFFFF:
            raise FormatError("Encoded integer exceeds the supported range.")
        return -value if first & 0x80 else value

    def count(self, label):
        count = self.signed()
        if count < 0 or count > (len(self.data) - self.pos) // 5:
            raise FormatError(f"Invalid {label} count.")
        return count


def signed_count(value):
    first = value & 0x3F
    value >>= 6
    data = bytearray([first | (0x40 if value else 0)])
    while value:
        byte = value & 0x7F
        value >>= 7
        data.append(byte | (0x80 if value else 0))
    return bytes(data)


@dataclass
class Record:
    encoded: bytes
    encrypted: bytes
    checksum: int


@dataclass
class Table:
    key: int
    mask: int
    header: bytes
    keys: dict
    records: dict


def checksum(raw):
    return sum(word for word, in struct.iter_unpack("<H", raw))


def inspect_table(data, slot):
    reader = Reader(data)
    version, high = reader.unpack("<IH")
    if version != FORMAT_VERSION:
        raise FormatError(f"{slot}: unsupported .w2strings version {version}.")
    key, mask = LOCALE_KEYS[slot]
    if high != key >> 16:
        raise FormatError(f"{slot}: unexpected locale key.")
    keys = {}
    for _ in range(reader.count("key")):
        length = reader.signed()
        name = reader.read(-length).decode("cp1252") if length < 0 else reader.read(length * 2).decode("utf-16le")
        index, = reader.unpack("<I")
        if not name or name in keys:
            raise FormatError(f"{slot}: empty or duplicate key name.")
        keys[name] = index ^ mask
    low, = reader.unpack("<H")
    if low != key & 0xFFFF:
        raise FormatError(f"{slot}: unexpected locale key.")
    header = data[:reader.pos]
    stored, = reader.unpack("<I")
    records = {}
    total = 0
    for _ in range(reader.count("text")):
        start = reader.pos
        index, = reader.unpack("<I")
        index ^= mask
        length = reader.signed()
        if length < 0:
            packed = reader.read(-length)
            expanded = bytearray(len(packed) * 2)
            expanded[::2] = packed
            raw = bytes(expanded)
        else:
            raw = reader.read(length * 2)
        if index in records:
            raise FormatError(f"{slot}: duplicate text ID {index}.")
        part = checksum(raw)
        records[index] = Record(data[start:reader.pos], raw, part)
        total += part
    if reader.pos != len(data) or not records or set(keys.values()) - records.keys():
        raise FormatError(f"{slot}: inconsistent text table or trailing data.")
    if (total & 0xFFFFFFFF) != stored ^ mask:
        raise FormatError(f"{slot}: .w2strings checksum mismatch.")
    return Table(key, mask, header, keys, records)


@lru_cache(maxsize=2048)
def text_mask(mask, count):
    # Same v201 rotation as the original Turkish adapter.
    seed = (mask >> 8) & 0xFFFF
    words = bytearray(count * 2)
    for offset in range(0, len(words), 2):
        struct.pack_into("<H", words, offset, ((count + 1) * seed) & 0xFFFF)
        seed = ((seed << 1) | (seed >> 15)) & 0xFFFF
    return int.from_bytes(words, "little")


def transform_text(raw, mask):
    if len(raw) > 16384:
        stream = text_mask.__wrapped__(mask, len(raw) // 2)
    else:
        stream = text_mask(mask, len(raw) // 2)
    return (int.from_bytes(raw, "little") ^ stream).to_bytes(len(raw), "little")


def xml_char_ok(cp):
    return cp in (9, 10, 13) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


def parse_gibbed_xml(path, table):
    raw = path.read_bytes().decode("utf-8-sig")

    def remove_forbidden_reference(match):
        token = match[0]
        code = int(token[3:-1], 16) if token[2] == "x" else int(token[2:-1])
        return token if xml_char_ok(code) else ""

    # Permit inspection of Gibbed's stock XML containing forbidden controls.
    # The binary reader retains the original characters.
    raw = XML_REFERENCE.sub(remove_forbidden_reference, raw)
    root = ET.fromstring(raw)
    if root.tag != "strings" or root.attrib != {"version": str(FORMAT_VERSION), "encryption_key": str(table.key)}:
        raise FormatError("Gibbed returned unexpected localization metadata.")
    key_nodes = root.findall("./keys/key")
    text_nodes = root.findall("./texts/text")
    keys = {node.attrib["id"]: int(node.text) for node in key_nodes}
    texts = {int(node.attrib["id"]): node.text or "" for node in text_nodes}
    if (len(key_nodes) != len(keys) or len(text_nodes) != len(texts)
            or keys != table.keys or texts.keys() != table.records.keys()
            or any(len(node) for node in text_nodes)):
        raise FormatError("Gibbed returned different keys, text IDs, or an unexpected XML record.")
    return texts


def run_gibbed(exe, option, source, target):
    completed = subprocess.run(
        [str(exe), option, str(source), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not target.is_file() or not target.stat().st_size:
        detail = (completed.stderr or completed.stdout).strip()
        raise FormatError(f"Gibbed {option} failed for {source.name}.\n{detail}")


@dataclass
class Decoded:
    table: Table
    texts: dict
    opaque: dict
    marked_ids: set
    xml_controls: set
    carriage_returns: set


def decode_source(data, slot, gibbed, work):
    table = inspect_table(data, slot)
    projected = None
    if slot != "tr0":
        source = work / (slot + ".w2strings")
        xml = work / (slot + ".xml")
        source.write_bytes(data)
        run_gibbed(gibbed, "-d", source, xml)
        projected = parse_gibbed_xml(xml, table)

    texts, opaque = {}, {}
    marked, xml_controls, carriage_returns = set(), set(), set()
    for index, record in table.records.items():
        xml_text = projected[index] if projected is not None else None
        if xml_text and SEPARATOR in xml_text:
            marked.add(index)
        try:
            original = transform_text(record.encrypted, table.mask).decode("utf-16le")
        except UnicodeDecodeError:
            opaque[index] = "invalid_utf16"
            continue
        if SEPARATOR in original:
            marked.add(index)
        filtered = original
        if XML_FORBIDDEN.search(original):
            xml_controls.add(index)
            filtered = XML_FORBIDDEN.sub("", original)
        if "\r" in original:
            carriage_returns.add(index)
        if projected is not None:
            # Compare the XML projection, then use the untouched binary text.
            normalized = filtered.replace("\r\n", "\n").replace("\r", "\n")
            if xml_text not in (original, filtered, normalized):
                opaque[index] = "gibbed_text_disagrees"
                continue
        texts[index] = original
    return Decoded(table, texts, opaque, marked, xml_controls, carriage_returns)


def reconcile_keys(primary, secondary):
    pkeys, skeys = primary.keys, secondary.keys
    common = pkeys.keys() & skeys.keys()
    conflicts = {name for name in common if pkeys[name] != skeys[name]}
    if len(common) - len(conflicts) <= len(conflicts):
        raise FormatError("The two language tables lack consistent shared ID mappings.")

    pnames, snames = {}, {}
    for keys, names in ((pkeys, pnames), (skeys, snames)):
        for name, index in keys.items():
            names.setdefault(index, set()).add(name)

    pairs, ambiguous = {}, set()
    for name in conflicts:
        left, right = pkeys[name], skeys[name]
        if left not in secondary.records and right not in primary.records and pnames[left] == snames[right]:
            pairs[left] = right
        else:
            ambiguous.update((left, right))
    return pairs, ambiguous


@dataclass
class MergePlan:
    changes: dict
    missing: int
    empty: int
    already: int
    protected: int
    key_matched: int
    opaque_kept: int
    ambiguous_kept: int


@dataclass
class MergeResult:
    data: bytes
    changed: int
    missing: int
    empty: int
    already: int
    protected: int
    total: int
    key_matched: int
    opaque_kept: int
    ambiguous_kept: int
    native_encoded: int


def plan_merge(primary, secondary, base):
    pairs, ambiguous = reconcile_keys(primary.table, secondary.table)
    if base is primary:
        donor_ids, target, donor = pairs, primary, secondary
    elif base is secondary:
        donor_ids, target, donor = {right: left for left, right in pairs.items()}, secondary, primary
    else:
        raise FormatError("The target must be one of the two source tables.")

    changes = {}
    missing = empty = already = key_matched = opaque_kept = ambiguous_kept = 0
    for index in target.table.records:
        donor_index = donor_ids.get(index, index)
        if index in ambiguous:
            ambiguous_kept += 1
            continue
        if index in target.opaque or donor_index in donor.opaque:
            opaque_kept += 1
            continue
        target_text, donor_text = target.texts[index], donor.texts.get(donor_index)
        if not target_text.strip():
            empty += 1
        elif donor_text is None:
            missing += 1
        elif not donor_text.strip():
            empty += 1
        elif target_text == donor_text:
            already += 1
        else:
            p, s = (target_text, donor_text) if base is primary else (donor_text, target_text)
            changes[index] = p + SEPARATOR + s
            key_matched += donor_index != index

    return MergePlan(changes, missing, empty, already, opaque_kept + ambiguous_kept, key_matched,
                     opaque_kept, ambiguous_kept)


def merge_pair(primary, secondary, base, target_slot, gibbed, work):
    plan = plan_merge(primary, secondary, base)
    if not plan.changes:
        raise FormatError("No compatible bilingual records were produced.")
    data, total_records, native_encoded = assemble_changes(base.table, plan.changes, target_slot, gibbed, work)
    return MergeResult(data, len(plan.changes), plan.missing, plan.empty, plan.already, plan.protected,
                       total_records, plan.key_matched, plan.opaque_kept, plan.ambiguous_kept, native_encoded)


def assemble_changes(base, changes, target_slot, gibbed, work):
    records, native_encoded = encode_changes(base, changes, target_slot, gibbed, work)
    output = bytearray(base.header)
    total = sum(record.checksum for record in records.values())
    output += struct.pack("<I", (total & 0xFFFFFFFF) ^ base.mask)
    output += signed_count(len(records))
    for record in records.values():
        output += record.encoded

    data = bytes(output)
    verified = inspect_table(data, target_slot)
    if verified.header != base.header or verified.records != records:
        raise FormatError("The prepared localization failed final verification.")
    return data, len(records), native_encoded


def encode_changes(table, changes, slot, gibbed, work):
    records = table.records.copy()
    via_xml = {}
    for index, value in changes.items():
        if slot != "tr0" and "\r" not in value and not XML_FORBIDDEN.search(value):
            via_xml[index] = value
            continue
        # XML cannot preserve these controls. Use the verified v201 transform
        # directly, as for every record in the Turkish adapter.
        raw = transform_text(value.encode("utf-16le"), table.mask)
        encoded = struct.pack("<I", index ^ table.mask) + signed_count(len(raw) // 2) + raw
        records[index] = Record(encoded, raw, checksum(raw))

    if via_xml:
        xml = work / "changes.xml"
        output = work / "changes.w2strings"
        root = ET.Element("strings", {"version": str(FORMAT_VERSION), "encryption_key": str(table.key)})
        ET.SubElement(root, "keys")
        texts = ET.SubElement(root, "texts")
        for index, value in via_xml.items():
            ET.SubElement(texts, "text", {"id": str(index)}).text = value
        xml.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        run_gibbed(gibbed, "-e", xml, output)
        delta = inspect_table(output.read_bytes(), slot)
        if delta.keys or delta.records.keys() != via_xml.keys():
            raise FormatError("Gibbed encoded an unexpected set of keys or text IDs.")
        records.update(delta.records)
    for index, value in changes.items():
        if transform_text(records[index].encrypted, table.mask).decode("utf-16le") != value:
            raise FormatError(f"The encoder did not preserve the expected Unicode text for ID {index}.")
    return records, len(changes) - len(via_xml)
