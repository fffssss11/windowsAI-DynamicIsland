# -*- coding: utf-8 -*-
"""把别的软件的窗口“悬挂”到屏幕顶部：缩小、置顶、在灵动岛下方排成一排（最多 3 个）；
结束悬挂时恢复成原来的位置、大小、最大化状态和置顶状态。

- DragWatcher：系统级监听“开始/结束拖动窗口”，用来判断窗口是不是被拖到了灵动岛上。
- HangManager：悬挂 / 还原 / 排列；状态写进 hung_windows.json，程序意外退出后下次启动先把窗口还原。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import threading

from PIL import Image, ImageChops

import timezone_float as tf

user32, kernel32 = tf.user32, tf.kernel32
gdi32 = ctypes.WinDLL('gdi32')
dwmapi = ctypes.WinDLL('dwmapi')

MAX_HANG = 3
STATE_PATH = os.path.join(tf.DATA_DIR, 'hung_windows.json')
HWND_TOPMOST, HWND_NOTOPMOST = wt.HWND(-1), wt.HWND(-2)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
SW_RESTORE = 9
WS_CAPTION, WS_EX_TOPMOST = 0x00C00000, 0x00000008


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [('length', wt.UINT), ('flags', wt.UINT), ('showCmd', wt.UINT), ('ptMinPosition', wt.POINT),
                ('ptMaxPosition', wt.POINT), ('rcNormalPosition', wt.RECT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

user32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_size_t
user32.GetClassLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetClassLongPtrW.restype = ctypes.c_size_t
user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON, ctypes.c_int, ctypes.c_int, wt.UINT,
                              wt.HBRUSH, wt.UINT]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
user32.SetWinEventHook.restype = wt.HANDLE
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]


# ---------------------------------------------------------------- 窗口信息
def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def is_topmost(hwnd):
    return bool(user32.GetWindowLongPtrW(hwnd, -20) & WS_EX_TOPMOST)


def get_placement(hwnd):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    r = wp.rcNormalPosition
    return {'flags': wp.flags, 'showCmd': wp.showCmd, 'min': [wp.ptMinPosition.x, wp.ptMinPosition.y],
            'max': [wp.ptMaxPosition.x, wp.ptMaxPosition.y], 'normal': [r.left, r.top, r.right, r.bottom]}


def set_placement(hwnd, p):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    wp.flags = p['flags']
    wp.showCmd = p['showCmd'] if p['showCmd'] in (1, 3) else 1  # 正常 / 最大化；别的一律按正常还原
    wp.ptMinPosition = wt.POINT(*p['min'])
    wp.ptMaxPosition = wt.POINT(*p['max'])
    wp.rcNormalPosition = wt.RECT(*p['normal'])
    user32.SetWindowPlacement(hwnd, ctypes.byref(wp))


def frame_margins(hwnd):
    """窗口矩形和肉眼看到的边框之间的差（Win10/11 窗口四周有一圈看不见的拖拽边）。"""
    r, v = wt.RECT(), wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(v), ctypes.sizeof(v)) == 0:  # EXTENDED_FRAME_BOUNDS
        return v.left - r.left, v.top - r.top, r.right - v.right, r.bottom - v.bottom
    return 0, 0, 0, 0


def visual_rect(hwnd):
    v = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(v), ctypes.sizeof(v)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(v))
    return v.left, v.top, v.right, v.bottom


def _render_icon(hicon, n, bg):
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), n, -n, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    try:
        ctypes.memset(bits, bg, n * n * 4)
        old = gdi32.SelectObject(mem, hbmp)
        user32.DrawIconEx(mem, 0, 0, hicon, n, n, 0, None, 3)  # DI_NORMAL
        data = ctypes.string_at(bits, n * n * 4)
        gdi32.SelectObject(mem, old)
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
    return Image.frombuffer('RGBA', (n, n), data, 'raw', 'BGRA', 0, 1).convert('RGB')


def window_icon(hwnd, n):
    """窗口图标转成 PIL 图（RGBA）。分别画在黑底和白底上，两张一比就得到透明度，老式图标也能用。"""
    out = ctypes.c_size_t()
    hicon = 0
    for which in (1, 2, 0):  # ICON_BIG, ICON_SMALL2, ICON_SMALL
        if user32.SendMessageTimeoutW(hwnd, 0x007F, which, 0, 0x0002, 150, ctypes.byref(out)) and out.value:
            hicon = out.value
            break
    if not hicon:
        hicon = user32.GetClassLongPtrW(hwnd, -14) or user32.GetClassLongPtrW(hwnd, -34)  # GCLP_HICON / HICONSM
    if not hicon:
        return None
    try:
        black, white = _render_icon(hicon, n, 0x00), _render_icon(hicon, n, 0xFF)
    except Exception:
        return None
    alpha = ImageChops.invert(ImageChops.subtract(white, black).convert('L'))
    icon = black.convert('RGBA')
    icon.putalpha(alpha)
    return icon if alpha.getbbox() else None


# ---------------------------------------------------------------- 拖动监听
class DragWatcher(threading.Thread):
    """系统级监听窗口开始 / 结束拖动（标题栏拖动和拉边框都算），回调 on_event('drag_start'|'drag_end', hwnd)。"""

    def __init__(self, on_event):
        super().__init__(daemon=True)
        self.on_event = on_event

    def run(self):
        self._proc = WINEVENTPROC(self._cb)  # 要一直引用着，不然回调被回收
        # EVENT_SYSTEM_MOVESIZESTART..END；WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        user32.SetWinEventHook(0x000A, 0x000B, None, self._proc, 0, 0, 0x0000 | 0x0002)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _cb(self, hook, event, hwnd, id_object, id_child, thread, ms):
        if id_object == 0 and hwnd:  # OBJID_WINDOW
            self.on_event('drag_start' if event == 0x000A else 'drag_end', hwnd)


# ---------------------------------------------------------------- 悬挂管理
class HangItem:
    def __init__(self, hwnd, placement, topmost, pid, title, icon):
        self.hwnd, self.placement, self.topmost = hwnd, placement, topmost
        self.pid, self.title, self.icon = pid, title, icon
        self.size = None  # 悬挂时的目标大小（肉眼可见部分）


class HangManager:
    def __init__(self, monitor, top_y, gap, icon_px):
        self.mon = monitor          # (left, top, right, bottom)
        self.top_y = top_y          # 悬挂窗口的上沿（灵动岛小条下面一点）
        self.gap = gap
        self.icon_px = icon_px
        self.items = []
        self.version = 0            # 变了就让灵动岛重画右侧按钮

    def full(self):
        return len(self.items) >= MAX_HANG

    def is_hung(self, hwnd):
        return any(i.hwnd == hwnd for i in self.items)

    def eligible(self, hwnd, own_pid):
        if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or self.is_hung(hwnd):
            return False
        if user32.GetAncestor(hwnd, tf.GA_ROOT) != hwnd or window_pid(hwnd) == own_pid:
            return False
        if tf.class_name(hwnd) in tf.SKIP_CLASSES - {'ConsoleWindowClass', 'CASCADIA_HOSTING_WINDOW_CLASS'}:
            return False  # 桌面、任务栏等不算；终端窗口可以悬挂
        return (user32.GetWindowLongPtrW(hwnd, -16) & WS_CAPTION) == WS_CAPTION

    def hang(self, hwnd, placement, topmost):
        """返回 (成功?, 失败原因)。"""
        if self.full():
            return False, f'最多悬挂 {MAX_HANG} 个'
        if not user32.IsWindow(hwnd):
            return False, '窗口已经关了'
        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)  # 拖到顶部可能被系统顺手最大化了
        l, t, r, b = placement['normal']
        ow, oh = max(1, r - l), max(1, b - t)
        mw, mh = self.mon[2] - self.mon[0], self.mon[3] - self.mon[1]
        tw = min(ow, int(mw * 0.3))
        th = int(tw * oh / ow)
        if th > mh * 0.45:
            th = int(mh * 0.45)
            tw = int(th * ow / oh)
        ml, mt, mr, mb = frame_margins(hwnd)
        if not user32.SetWindowPos(hwnd, HWND_TOPMOST, self.mon[0], self.top_y, tw + ml + mr, th + mt + mb,
                                   SWP_NOACTIVATE | SWP_SHOWWINDOW):
            return False, '这个窗口不让悬挂（可能是管理员权限的程序）'
        item = HangItem(hwnd, placement, topmost, window_pid(hwnd), window_title(hwnd),
                        window_icon(hwnd, self.icon_px))
        item.size = (tw, th)
        self.items.append(item)
        self.layout()
        self.changed()
        return True, None

    def layout(self):
        """在灵动岛下方居中排成一排。软件有最小尺寸限制的，按它实际能缩到的大小排。"""
        sizes = []
        for it in self.items:
            ml, mt, mr, mb = frame_margins(it.hwnd)
            tw, th = it.size
            user32.SetWindowPos(it.hwnd, HWND_TOPMOST, 0, 0, tw + ml + mr, th + mt + mb, SWP_NOMOVE | SWP_NOACTIVATE)
            l, t, r, b = visual_rect(it.hwnd)
            sizes.append((r - l, b - t, ml, mt))
        total = sum(s[0] for s in sizes) + self.gap * max(0, len(sizes) - 1)
        x = max(self.mon[0] + self.gap, (self.mon[0] + self.mon[2]) // 2 - total // 2)
        for it, (vw, vh, ml, mt) in zip(self.items, sizes):
            user32.SetWindowPos(it.hwnd, HWND_TOPMOST, x - ml, self.top_y - mt, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
            x += vw + self.gap

    def _restore(self, it):
        if user32.IsWindow(it.hwnd) and window_pid(it.hwnd) == it.pid:
            user32.SetWindowPos(it.hwnd, HWND_TOPMOST if it.topmost else HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            set_placement(it.hwnd, it.placement)

    def release(self, hwnd):
        it = next((i for i in self.items if i.hwnd == hwnd), None)
        if it is None:
            return
        self.items.remove(it)
        self._restore(it)
        self.layout()
        self.changed()

    def release_all(self):
        items, self.items = self.items, []
        for it in items:
            self._restore(it)
        if items:
            self.changed()

    def prune(self):
        """悬挂中的窗口被用户关掉了：从列表里去掉。"""
        alive = [i for i in self.items if user32.IsWindow(i.hwnd) and window_pid(i.hwnd) == i.pid]
        if len(alive) != len(self.items):
            self.items = alive
            self.layout()
            self.changed()

    def changed(self):
        self.version += 1
        self.save()

    def save(self):
        try:
            if not self.items:
                if os.path.exists(STATE_PATH):
                    os.remove(STATE_PATH)
                return
            with open(STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump([{'hwnd': i.hwnd, 'pid': i.pid, 'placement': i.placement, 'topmost': i.topmost,
                            'title': i.title} for i in self.items], f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def recover(self):
        """上次没正常结束悬挂（程序崩溃、被强行关掉）：把那些窗口还原。"""
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for s in saved:
            self._restore(HangItem(s['hwnd'], s['placement'], s['topmost'], s['pid'], s.get('title', ''), None))
        try:
            os.remove(STATE_PATH)
        except OSError:
            pass
