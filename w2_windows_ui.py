"""Win32 window sizing, monitor placement and local game icons."""
import ctypes as C
from ctypes import wintypes as W
import struct

WM_SIZE, WM_GETMINMAXINFO = 0x0005, 0x0024
WM_NCLBUTTONDBLCLK, WM_SYSCOMMAND = 0x00A3, 0x0112
WM_WINDOWPOSCHANGING = 0x0046
SC_SIZE, SC_MAXIMIZE, SC_RESTORE = 0xF000, 0xF030, 0xF120
HTCAPTION, SIZE_MAXIMIZED = 2, 2
WS_THICKFRAME, WS_MAXIMIZEBOX = 0x00040000, 0x00010000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x01, 0x02, 0x04
SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x10, 0x20


class MonitorInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("monitor", W.RECT), ("work", W.RECT), ("flags", W.DWORD)]


class MinMaxInfo(C.Structure):
    _fields_ = [(name, W.POINT) for name in ("reserved", "max_size", "max_position", "min_track", "max_track")]

class WindowPos(C.Structure):
    _fields_ = [("hwnd", W.HWND), ("after", W.HWND), ("x", C.c_int), ("y", C.c_int),
                ("width", C.c_int), ("height", C.c_int), ("flags", W.UINT)]



def monitor_work_area():
    user = C.WinDLL("user32", use_last_error=True)
    cursor = W.POINT()
    native(user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))(C.byref(cursor))
    monitor = native(user, "MonitorFromPoint", W.HMONITOR, W.POINT, W.DWORD)(cursor, 2)
    info = MonitorInfo(size=C.sizeof(MonitorInfo))
    if not native(user, "GetMonitorInfoW", W.BOOL, W.HMONITOR, C.POINTER(MonitorInfo))(monitor, C.byref(info)):
        raise C.WinError(C.get_last_error())
    rect = info.work
    return rect.left, rect.top, rect.right, rect.bottom


def centered_origin(work, width, height):
    left, top, right, bottom = work
    return left + max(0, (right - left - width) // 2), top + max(0, (bottom - top - height) // 2)


def fixed_window_style(style):
    return style & ~(WS_THICKFRAME | WS_MAXIMIZEBOX)


def blocks_resize_message(message, command):
    return (message == WM_SYSCOMMAND and (command & 0xFFF0) in (SC_SIZE, SC_MAXIMIZE)
            or message == WM_NCLBUTTONDBLCLK and command == HTCAPTION)


def constrain_window_position(position, width, height, minimized):
    if not minimized and not position.flags & SWP_NOSIZE:
        position.width, position.height = width, height


def lock_and_center_window(window, work):
    user = C.WinDLL("user32", use_last_error=True)
    controls = C.WinDLL("comctl32", use_last_error=True)
    hwnd = native(user, "GetAncestor", W.HWND, W.HWND, W.UINT)(window.winfo_id(), 2)
    get_style = native(user, "GetWindowLongW", W.LONG, W.HWND, C.c_int)
    set_style = native(user, "SetWindowLongW", W.LONG, W.HWND, C.c_int, W.LONG)
    place = native(user, "SetWindowPos", W.BOOL, W.HWND, W.HWND,
                   C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
    get_rect = native(user, "GetWindowRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
    iconic = native(user, "IsIconic", W.BOOL, W.HWND)
    post = native(user, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    result = C.c_ssize_t
    callback_type = C.WINFUNCTYPE(result, W.HWND, W.UINT, W.WPARAM, W.LPARAM, C.c_size_t, C.c_size_t)
    default = native(controls, "DefSubclassProc", result, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    subclass = native(controls, "SetWindowSubclass", W.BOOL, W.HWND, callback_type, C.c_size_t, C.c_size_t)

    style = get_style(hwnd, -16)
    set_style(hwnd, -16, fixed_window_style(style))
    fixed_flags = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSIZE
    if not place(hwnd, None, 0, 0, 0, 0, fixed_flags | SWP_FRAMECHANGED | SWP_NOMOVE):
        raise C.WinError(C.get_last_error())
    rect = W.RECT()
    if not get_rect(hwnd, C.byref(rect)):
        raise C.WinError(C.get_last_error())
    width, height = rect.right - rect.left, rect.bottom - rect.top

    @callback_type
    def fixed_proc(handle, message, wparam, lparam, _id, _data):
        if blocks_resize_message(message, wparam):
            return 0
        if message == WM_GETMINMAXINFO:
            default(handle, message, wparam, lparam)
            info = MinMaxInfo.from_address(lparam)
            info.min_track = info.max_track = info.max_size = W.POINT(width, height)
            return 0
        if message == WM_WINDOWPOSCHANGING:
            answer = default(handle, message, wparam, lparam)
            constrain_window_position(WindowPos.from_address(lparam), width, height, iconic(handle))
            return answer
        if message == WM_SIZE and wparam == SIZE_MAXIMIZED:
            post(handle, WM_SYSCOMMAND, SC_RESTORE, 0)
        return default(handle, message, wparam, lparam)

    if not subclass(hwnd, fixed_proc, 1, 0):
        raise C.WinError(C.get_last_error())
    window._fixed_window_proc = fixed_proc
    x, y = centered_origin(work, width, height)
    if not place(hwnd, None, x, y, 0, 0, fixed_flags):
        raise C.WinError(C.get_last_error())


def enable_system_dpi():
    user = C.WinDLL("user32")
    try:
        set_context = user.SetProcessDpiAwarenessContext
    except AttributeError:
        user.SetProcessDPIAware()
    else:
        set_context.argtypes = (C.c_void_p,)
        set_context.restype = W.BOOL
        set_context(C.c_void_p(-2))  # SYSTEM_AWARE; also declared in the EXE manifest.


def native(library, name, result, *arguments):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


def load_game_icon(game, size):
    for executable in (game / "Launcher.exe", game / "bin/witcher2.exe"):
        if executable.is_file():
            icon = read_executable_icon(executable, size)
            if icon:
                return icon
    return None


def read_executable_icon(executable, size):
    shell, user, gdi = (C.WinDLL(name) for name in ("shell32", "user32", "gdi32"))
    extract = native(shell, "ExtractIconExW", W.UINT, W.LPCWSTR, C.c_int,
                     C.POINTER(W.HICON), C.POINTER(W.HICON), W.UINT)
    destroy = native(user, "DestroyIcon", W.BOOL, W.HICON)
    draw = native(user, "DrawIconEx", W.BOOL, W.HDC, C.c_int, C.c_int,
                  W.HICON, C.c_int, C.c_int, W.UINT, W.HBRUSH, W.UINT)
    create_dc = native(gdi, "CreateCompatibleDC", W.HDC, W.HDC)
    delete_dc = native(gdi, "DeleteDC", W.BOOL, W.HDC)
    create_bitmap = native(gdi, "CreateDIBSection", W.HBITMAP, W.HDC, C.c_void_p,
                           W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD)
    select = native(gdi, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    delete = native(gdi, "DeleteObject", W.BOOL, W.HGDIOBJ)
    icon = W.HICON()
    if extract(str(executable), 0, C.byref(icon), None, 1) != 1 or not icon:
        return None
    dc = bitmap = previous = None
    try:
        dc = create_dc(None)
        if not dc:
            return None
        header = C.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, size, -size, 1, 32, 0,
                                                    size * size * 4, 0, 0, 0, 0))
        pixels = C.c_void_p()
        bitmap = create_bitmap(dc, header, 0, C.byref(pixels), None, 0)
        if not bitmap or not pixels:
            return None
        previous = select(dc, bitmap)
        if not previous or previous == C.c_void_p(-1).value:
            previous = None
            return None
        C.memmove(pixels, bytes((25, 22, 24, 255)) * (size * size), size * size * 4)
        if not draw(dc, 0, 0, icon, size, size, 0, None, 3):
            return None
        raw = C.string_at(pixels, size * size * 4)
        rgb = bytearray(size * size * 3)
        rgb[::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[::4]
        return f"P6\n{size} {size}\n255\n".encode("ascii") + rgb
    finally:
        if previous:
            select(dc, previous)
        if bitmap:
            delete(bitmap)
        if dc:
            delete_dc(dc)
        destroy(icon)
