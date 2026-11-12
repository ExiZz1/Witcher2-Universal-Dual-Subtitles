"""Audit bundled PE dependencies and observe the CLR used by Gibbed."""
import argparse
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import time

import pefile
from smoke_p3 import clean_environment


def fingerprints(folder):
    return {p.relative_to(folder).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def observe_clr(exe, source, cwd):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD)
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = (W.HANDLE,)
    kernel.CloseHandle.restype = W.BOOL
    modules = kernel.K32EnumProcessModulesEx
    modules.argtypes = (W.HANDLE, C.POINTER(W.HMODULE), W.DWORD, C.POINTER(W.DWORD), W.DWORD)
    modules.restype = W.BOOL
    module_path = kernel.K32GetModuleFileNameExW
    module_path.argtypes = (W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD)
    module_path.restype = W.DWORD
    process = subprocess.Popen([str(exe), "-d", str(source), str(cwd / "decoded.xml")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                               env=clean_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
    paths = set()
    handle = kernel.OpenProcess(0x0410, False, process.pid)
    deadline = time.monotonic() + 30
    try:
        while handle and process.poll() is None and time.monotonic() < deadline:
            handles, count = (W.HMODULE * 256)(), W.DWORD()
            if modules(handle, handles, C.sizeof(handles), C.byref(count), 3):
                for item in handles[:min(len(handles), count.value // C.sizeof(W.HMODULE))]:
                    text = C.create_unicode_buffer(1024)
                    if module_path(handle, item, text, len(text)):
                        location = Path(text.value)
                        if location.name.lower() in ("clr.dll", "mscorwks.dll", "mscoree.dll", "coreclr.dll"):
                            paths.add(str(location))
            time.sleep(0.01)
        _, err = process.communicate(timeout=30)
    finally:
        if handle:
            kernel.CloseHandle(handle)
    assert process.returncode == 0, err
    assert (cwd / "decoded.xml").stat().st_size > 0
    assert any("\\v4.0.30319\\clr.dll" in name.lower() for name in paths), paths
    assert not any(Path(name).name.lower() in ("mscorwks.dll", "coreclr.dll") for name in paths), paths
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("rebuild", type=Path)
    parser.add_argument("locale", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.package.resolve(strict=True)
    hashes = fingerprints(root)
    assert hashes == fingerprints(args.rebuild.resolve(strict=True)), "Rebuild differs"
    environment = clean_environment()
    system = Path(environment["SYSTEMROOT"])
    system_roots = (system / "System32", system / "SysWOW64")
    private_names = {Path(name).name.lower() for name in hashes}
    external, managed = set(), {}
    for relative in hashes:
        file = root / relative
        if file.suffix.lower() not in (".exe", ".dll", ".pyd"):
            continue
        pe = pefile.PE(str(file), fast_load=True)
        pe.parse_data_directories(directories=[1, 13])
        for entry in (*getattr(pe, "DIRECTORY_ENTRY_IMPORT", ()), *getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", ())):
            name = entry.dll.decode("ascii").lower()
            if name not in private_names:
                external.add(name)
                assert (name.startswith(("api-ms-", "ext-ms-"))
                        or any((folder / name).is_file() for folder in system_roots)), (relative, name)
        clr = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14].VirtualAddress
        if clr:
            metadata_rva = pe.get_dword_at_rva(clr + 8)
            assert pe.get_data(metadata_rva, 4) == b"BSJB"
            length = pe.get_dword_at_rva(metadata_rva + 12)
            managed[file.name] = pe.get_data(metadata_rva + 16, length).split(b"\0", 1)[0].decode()
        pe.close()
    assert all(managed[name].startswith("v4.") for name in
               ("Gibbed.RED.Strings.exe", "Gibbed.RED.Unpack.exe", "Gibbed.IO.dll", "Gibbed.RED.FileFormats.dll"))
    assert {"python313.dll", "tcl86t.dll", "tk86t.dll", "freetype.dll", "vcruntime140.dll"} <= private_names
    with tempfile.TemporaryDirectory(prefix="w2-clr-") as directory:
        clr = observe_clr(root / "_internal/Gibbed RED Tools/Gibbed.RED.Strings.exe",
                          args.locale.resolve(strict=True), Path(directory))
    result = {"status": "ok", "byte_identical_rebuild_files": len(hashes),
              "exe_sha256": hashes["Witcher2_Universal_Dual_Subtitles.exe"],
              "os_native_imports": sorted(external), "managed_metadata_versions": managed, "gibbed_loaded_clr": clr}
    args.output.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print("PASS PE imports and OS CLR v4; byte-identical repeat build:", len(hashes), "files")


if __name__ == "__main__":
    main()
