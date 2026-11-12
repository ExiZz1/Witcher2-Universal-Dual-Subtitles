"""Stage hash-locked third-party binaries from local upstream downloads."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
TEST_TOOL = "Gibbed.RED.Pack.exe"


def lock(root):
    return json.loads((root / "third-party-lock.json").read_text("utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_dependencies(root):
    for relative, digest in lock(root).items():
        if relative.endswith("/" + TEST_TOOL):
            continue
        file = root / relative
        if not file.is_file() or sha(file.read_bytes()) != digest:
            raise ValueError(f"Missing or unexpected build dependency {relative}. Run prepare_dependencies.py.")


def candidates(source, name, size_limit):
    if source.is_dir():
        for folder in (source, source / "bin", source / "Gibbed RED Tools", source / "resources",
                       source / "_internal/Gibbed RED Tools", source / "_internal/resources"):
            file = folder / name
            if file.is_file() and file.stat().st_size <= size_limit:
                yield file.read_bytes()
    elif source.suffix.casefold() == ".zip":
        with ZipFile(source) as archive:
            for item in archive.infolist():
                if Path(item.filename).name.casefold() == name.casefold() and item.file_size <= size_limit:
                    yield archive.read(item)
    elif source.name.casefold() == name.casefold() and source.is_file() and source.stat().st_size <= size_limit:
        yield source.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gibbed", required=True, type=Path, help="Gibbed directory or ZIP")
    parser.add_argument("--freetype", required=True, type=Path, help="Oracle JDK ZIP, JDK folder or freetype.dll")
    parser.add_argument("--test-tools", action="store_true", help="Also stage Gibbed.RED.Pack.exe for integration tests")
    args = parser.parse_args()
    verified = {}
    for relative, digest in lock(ROOT).items():
        name = Path(relative).name
        if name == TEST_TOOL and not args.test_tools:
            continue
        source = args.freetype if name == "freetype.dll" else args.gibbed
        data = next((candidate for candidate in candidates(source, name, 4 * 1024 * 1024)
                     if sha(candidate) == digest), None)
        if data is None:
            parser.error(f"{name} is missing or does not match third-party-lock.json. See BUILD_GUIDE.md.")
        verified[ROOT / relative] = data
    for file, data in verified.items():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    validate_dependencies(ROOT)
    print(f"Staged {len(verified)} verified third-party dependencies.")


if __name__ == "__main__":
    main()
