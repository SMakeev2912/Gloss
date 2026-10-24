"""ctypes-объявления Win32 API, которые использует Gloss.

У всех функций явно заданы argtypes/restype: без этого на 64-битном Python
хэндлы обрезаются до 32 бит и вызовы ломаются в случайных местах.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
HGLOBAL = ctypes.c_void_p


def _fn(dll, name, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


# --- Сообщения и хоткеи -----------------------------------------------------

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP = 0x8000
PM_NOREMOVE = 0x0000

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

HWND_MESSAGE = wintypes.HWND(-3)
HWND_TOPMOST = wintypes.HWND(-1)

RegisterHotKey = _fn(user32, "RegisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
UnregisterHotKey = _fn(user32, "UnregisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
GetMessageW = _fn(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
PeekMessageW = _fn(user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
TranslateMessage = _fn(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
PostThreadMessageW = _fn(user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
CreateWindowExW = _fn(
    user32, "CreateWindowExW", wintypes.HWND,
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
DestroyWindow = _fn(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wintypes.DWORD)

# --- Клавиатура --------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT нужен, даже если не используется: он задаёт размер структуры.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


SendInput = _fn(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", wintypes.SHORT, ctypes.c_int)
MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", wintypes.UINT, wintypes.UINT, wintypes.UINT)

# --- Окна и мониторы ---------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


GetForegroundWindow = _fn(user32, "GetForegroundWindow", wintypes.HWND)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
GetCursorPos = _fn(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
MonitorFromPoint = _fn(user32, "MonitorFromPoint", wintypes.HANDLE, wintypes.POINT, wintypes.DWORD)
GetMonitorInfoW = _fn(user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
SetWindowPos = _fn(
    user32, "SetWindowPos", wintypes.BOOL,
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
)
ShowWindow = _fn(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
MessageBoxW = _fn(user32, "MessageBoxW", ctypes.c_int, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)

if hasattr(user32, "GetWindowLongPtrW"):
    GetWindowLongPtrW = _fn(user32, "GetWindowLongPtrW", LONG_PTR, wintypes.HWND, ctypes.c_int)
    SetWindowLongPtrW = _fn(user32, "SetWindowLongPtrW", LONG_PTR, wintypes.HWND, ctypes.c_int, LONG_PTR)
else:  # 32-битный Python
    GetWindowLongPtrW = _fn(user32, "GetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int)
    SetWindowLongPtrW = _fn(user32, "SetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int, wintypes.LONG)

# --- Буфер обмена и память ---------------------------------------------------

GMEM_MOVEABLE = 0x0002

OpenClipboard = _fn(user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
CloseClipboard = _fn(user32, "CloseClipboard", wintypes.BOOL)
EmptyClipboard = _fn(user32, "EmptyClipboard", wintypes.BOOL)
GetClipboardData = _fn(user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
SetClipboardData = _fn(user32, "SetClipboardData", wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
EnumClipboardFormats = _fn(user32, "EnumClipboardFormats", wintypes.UINT, wintypes.UINT)
IsClipboardFormatAvailable = _fn(user32, "IsClipboardFormatAvailable", wintypes.BOOL, wintypes.UINT)
GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", wintypes.DWORD)
RegisterClipboardFormatW = _fn(user32, "RegisterClipboardFormatW", wintypes.UINT, wintypes.LPCWSTR)
GetClipboardFormatNameW = _fn(user32, "GetClipboardFormatNameW", ctypes.c_int, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int)

GlobalAlloc = _fn(kernel32, "GlobalAlloc", HGLOBAL, wintypes.UINT, ctypes.c_size_t)
GlobalLock = _fn(kernel32, "GlobalLock", ctypes.c_void_p, HGLOBAL)
GlobalUnlock = _fn(kernel32, "GlobalUnlock", wintypes.BOOL, HGLOBAL)
GlobalSize = _fn(kernel32, "GlobalSize", ctypes.c_size_t, HGLOBAL)
GlobalFree = _fn(kernel32, "GlobalFree", HGLOBAL, HGLOBAL)

# --- Процессы, мьютекс, токены -----------------------------------------------

ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation

CreateMutexW = _fn(kernel32, "CreateMutexW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
CloseHandle = _fn(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
OpenProcess = _fn(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", wintypes.HANDLE)
OpenProcessToken = _fn(advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
GetTokenInformation = _fn(
    advapi32, "GetTokenInformation", wintypes.BOOL,
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
)


# --- Job Object: дочерние процессы завершаются вместе с Gloss -------------------

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

CreateJobObjectW = _fn(kernel32, "CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
SetInformationJobObject = _fn(kernel32, "SetInformationJobObject", wintypes.BOOL,
                              wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
AssignProcessToJobObject = _fn(kernel32, "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)

_kill_on_close_job = None


def kill_with_parent(pid: int) -> bool:
    """Привязывает процесс к Job Object: Windows завершит его, когда закроется Gloss (даже при сбое)."""
    global _kill_on_close_job
    if _kill_on_close_job is None:
        job = CreateJobObjectW(None, None)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                  ctypes.byref(info), ctypes.sizeof(info)):
            return False
        _kill_on_close_job = job  # хэндл держим до конца процесса намеренно
    process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not process:
        return False
    try:
        return bool(AssignProcessToJobObject(_kill_on_close_job, process))
    finally:
        CloseHandle(process)


SYNCHRONIZE = 0x00100000
WaitForSingleObject = _fn(kernel32, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)


def wait_for_process(pid: int, timeout_ms: int) -> None:
    """Ждёт завершения процесса (или таймаута). Если процесса уже нет – сразу возвращается."""
    handle = OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        try:
            WaitForSingleObject(handle, timeout_ms)
        finally:
            CloseHandle(handle)


def _token_elevated(process_handle) -> bool | None:
    token = wintypes.HANDLE()
    if not OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevated = wintypes.DWORD()
        size = wintypes.DWORD()
        ok = GetTokenInformation(token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size))
        return bool(elevated.value) if ok else None
    finally:
        CloseHandle(token)


def is_current_process_elevated() -> bool:
    return bool(_token_elevated(GetCurrentProcess()))


def is_window_elevated(hwnd) -> bool:
    """True, если окно принадлежит процессу с правами администратора.

    Если токен процесса прочитать не удалось, считаем окно повышенным: у
    обычного процесса так бывает именно с окнами, запущенными от администратора.
    """
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return True
    try:
        elevated = _token_elevated(process)
        return True if elevated is None else elevated
    finally:
        CloseHandle(process)


def work_area_at_cursor() -> tuple[int, int, wintypes.RECT]:
    """Позиция курсора и рабочая область монитора под ним (без панели задач)."""
    pt = wintypes.POINT()
    GetCursorPos(ctypes.byref(pt))
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    GetMonitorInfoW(MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    return pt.x, pt.y, info.rcWork


def load_private_font(path: str) -> bool:
    """Подключает шрифт только для этого процесса: в системе он не появляется."""
    try:
        gdi32 = ctypes.WinDLL("gdi32")
        add = _fn(gdi32, "AddFontResourceExW", ctypes.c_int, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
        return add(path, 0x10, None) > 0  # FR_PRIVATE
    except (OSError, AttributeError):
        return False


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWCP_ROUND = 2


def colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def set_window_attribute(hwnd: int, attribute: int, value: int) -> bool:
    """DwmSetWindowAttribute для DWORD-значений. На старых Windows молча ничего не делает."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        func = _fn(dwm, "DwmSetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD, wintypes.LPVOID,
                   wintypes.DWORD)
        data = wintypes.DWORD(value)
        return func(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)) == 0
    except (OSError, AttributeError):
        return False


def dark_title_bar(hwnd: int, caption: str, text: str, border: str) -> None:
    """Тёмный заголовок окна; в Windows 11 ещё и в цвет окна, с тонкой рамкой."""
    set_window_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    set_window_attribute(hwnd, DWMWA_CAPTION_COLOR, colorref(caption))
    set_window_attribute(hwnd, DWMWA_TEXT_COLOR, colorref(text))
    set_window_attribute(hwnd, DWMWA_BORDER_COLOR, colorref(border))


def set_dpi_awareness() -> None:
    """Включает DPI-осведомлённость, чтобы координаты и шрифты были чёткими."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


def message_box(text: str, title: str, flags: int = 0x40) -> int:
    """Модальное окно сообщения (по умолчанию со значком «информация»)."""
    return MessageBoxW(None, text, title, flags | 0x00010000)  # MB_SETFOREGROUND


# --- Микрофон (waveIn) -----------------------------------------------------------

winmm = ctypes.WinDLL("winmm", use_last_error=True)

WAVE_MAPPER = 0xFFFFFFFF  # микрофон по умолчанию в Windows
WAVE_FORMAT_PCM = 1
CALLBACK_EVENT = 0x00050000
WHDR_DONE = 0x00000001
MMSYSERR_ALLOCATED = 4
MMSYSERR_BADDEVICEID = 2
MMSYSERR_NODRIVER = 6
HWAVEIN = wintypes.HANDLE


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ULONG_PTR),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ULONG_PTR),
    ]


CreateEventW = _fn(kernel32, "CreateEventW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
waveInOpen = _fn(winmm, "waveInOpen", wintypes.UINT, ctypes.POINTER(HWAVEIN), wintypes.UINT,
                 ctypes.POINTER(WAVEFORMATEX), ULONG_PTR, ULONG_PTR, wintypes.DWORD)
waveInPrepareHeader = _fn(winmm, "waveInPrepareHeader", wintypes.UINT, HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT)
waveInUnprepareHeader = _fn(winmm, "waveInUnprepareHeader", wintypes.UINT, HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT)
waveInAddBuffer = _fn(winmm, "waveInAddBuffer", wintypes.UINT, HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT)
waveInStart = _fn(winmm, "waveInStart", wintypes.UINT, HWAVEIN)
waveInReset = _fn(winmm, "waveInReset", wintypes.UINT, HWAVEIN)
waveInClose = _fn(winmm, "waveInClose", wintypes.UINT, HWAVEIN)
