"""Nonvisual verification of the shipped Windows runtime."""
import ctypes
import encodings
import json
from pathlib import Path
import sys
import tkinter
import winreg


def loaded_module(name):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint)
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    module = kernel.GetModuleHandleW(name)
    buffer = ctypes.create_unicode_buffer(32768)
    if not module or not kernel.GetModuleFileNameW(module, buffer, len(buffer)):
        raise RuntimeError(f"{name} is not loaded")
    return Path(buffer.value).resolve()


def write_runtime_report(path):
    try:
        root = Path(sys._MEIPASS).resolve()
        standard_library = Path(encodings.__file__).resolve()
        if not standard_library.is_file() or not standard_library.is_relative_to(root):
            raise RuntimeError("Python bootstrap modules resolved outside the package")
        tcl = tkinter.Tcl()
        tcl_library = Path(tcl.call("info", "library")).resolve()
        modules = {name: loaded_module(name) for name in
                   ("python313.dll", "_tkinter.pyd", "tcl86t.dll", "tk86t.dll")}
        if any(not p.is_relative_to(root) for p in (*modules.values(), tcl_library)):
            raise RuntimeError("A Python or Tcl/Tk runtime resolved outside the package")
        if not (root / "_tk_data/tk.tcl").is_file():
            raise RuntimeError("Bundled Tk scripts are missing")
        from w2_zh_ink import freetype
        freetype()
        modules["freetype.dll"] = loaded_module("freetype.dll")
        if not modules["freetype.dll"].is_relative_to(root):
            raise RuntimeError("FreeType resolved outside the package")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
                           0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            release, _ = winreg.QueryValueEx(key, "Release")
        result = {"status": "ok", "python": sys.version.split()[0], "tcl": tcl.call("info", "patchlevel"),
                  "tk_api": tkinter.TkVersion, "tcl_library": tcl_library.relative_to(root).as_posix(),
                  "modules": {name: loc.relative_to(root).as_posix() for name, loc in modules.items()},
                  "python_bootstrap": standard_library.relative_to(root).as_posix(),
                  "windows_build": sys.getwindowsversion().build, "os_net_framework_release": release}
    except Exception as error:
        result = {"status": "error", "message": str(error)}
    path.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    return int(result["status"] != "ok")
