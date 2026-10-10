# -*- coding: utf-8 -*-
"""时区悬浮窗

- 桌面置顶小窗显示当前时间，时区可选：洛杉矶 / 纽约 / 北京
- 划词换算：在任意软件里用鼠标选中一段含时间的文字（拖选或双击），
  在选区下方弹出它在其他时区是几点。文字里没写时区时，按悬浮窗当前时区理解。

操作：左键拖动移动窗口；左键单击切换时区；点“划词”开关划词换算；右键打开菜单。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime, timezone

import claude_usage as cu
import timeparse as tp

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 设置和运行状态放在用户目录，不放在程序旁边（打包成 exe 后程序目录是临时的；也免得把个人设置提交进仓库）
DATA_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'WindowsAI-Island')
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
_OLD_CONFIG = os.path.join(APP_DIR, 'config.json')
if not os.path.exists(CONFIG_PATH) and os.path.exists(_OLD_CONFIG):  # 老版本放在程序旁边，搬过去
    import shutil
    shutil.copyfile(_OLD_CONFIG, CONFIG_PATH)
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'TimezoneFloat'

BG, FG, SUB, ACCENT, BORDER = '#1e2227', '#f1f3f5', '#9aa3ad', '#5aa9ff', '#3a414a'
POP_BG, SRC_BG, OK_GREEN = '#262b31', '#2f3a48', '#4cd07d'
WARN, BAD, TRACK = '#f0b429', '#ff6b6b', '#363c44'
UI_FONT = 'Microsoft YaHei UI'
NUM_FONT = 'Segoe UI Semibold'

# ---------------------------------------------------------------- Win32
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]


user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetDoubleClickTime.restype = wt.UINT
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
user32.GetClipboardSequenceNumber.restype = wt.DWORD
user32.OpenClipboard.argtypes = [wt.HWND]
user32.EnumClipboardFormats.argtypes = [wt.UINT]
user32.EnumClipboardFormats.restype = wt.UINT
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalSize.argtypes = [wt.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE

VK_LBUTTON, VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_C = 0x01, 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x43
KEYEVENTF_KEYUP = 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GA_ROOT = 2
# 这些窗口里拖动不是在选文字，发 Ctrl+C 反而会复制文件 / 中断命令
SKIP_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'CabinetWClass', 'ExploreWClass',
                'ConsoleWindowClass', 'CASCADIA_HOSTING_WINDOW_CLASS', 'PseudoConsoleWindow',
                'Windows.UI.Core.CoreWindow', 'TaskListThumbnailWnd', 'NotifyIconOverflowWindow',
                'TopLevelWindowForOverflowXamlIsland', 'XamlExplorerHostIslandWindow'}
# 剪贴板里不是 HGLOBAL 的格式（位图句柄、图元文件等）和私有格式，无法按字节备份
SKIP_CLIP_FORMATS = {2, 3, 9, 14, 0x80, 0x81, 0x82, 0x83, 0x8E}
# OLE 内部格式存的是原程序里的指针，复制过来没意义
OLE_FORMATS = {'DataObject', 'Ole Private Data'}
user32.GetClipboardFormatNameW.argtypes = [wt.UINT, wt.LPWSTR, ctypes.c_int]


def clip_format_name(fmt):
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClipboardFormatNameW(fmt, buf, 128)
    return buf.value


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitor_rects(x, y):
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wt.POINT(x, y), 2), ctypes.byref(info))
    m, w = info.rcMonitor, info.rcWork
    return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_fullscreen(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    m = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom) == (m.left, m.top, m.right, m.bottom)


def open_clipboard():
    for _ in range(25):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_snapshot():
    """备份剪贴板里所有能按字节备份的格式。打不开返回 None。"""
    if not open_clipboard():
        return None
    data = []
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in SKIP_CLIP_FORMATS or 0x200 <= fmt <= 0x3FF or clip_format_name(fmt) in OLE_FORMATS:
                continue
            h = user32.GetClipboardData(fmt)
            size = kernel32.GlobalSize(h) if h else 0
            p = kernel32.GlobalLock(h) if size else None
            if not p:
                continue
            try:
                data.append((fmt, ctypes.string_at(p, size)))
            finally:
                kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    return data


def clipboard_restore(data):
    if not open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        # 恢复的内容不要再进 Win+V 剪贴板历史
        data = data + [(user32.RegisterClipboardFormatW('ExcludeClipboardContentFromMonitorProcessing'), b'\0\0\0\0')]
        for fmt, raw in data:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(raw), 1))
            p = kernel32.GlobalLock(h) if h else None
            if not p:
                continue
            ctypes.memmove(p, raw, len(raw))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()


def clipboard_text():
    if not open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        p = kernel32.GlobalLock(h) if h else None
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def send_ctrl_c():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


# 划词时我们会自己发 Ctrl+C 再还原剪贴板。busy=正在做；seq=做完后的剪贴板序号（这次变化不是用户复制的）
CLIP_GUARD = {'busy': False, 'seq': None}


# ---------------------------------------------------------------- 划词监听
class SelectionWatcher(threading.Thread):
    """后台轮询鼠标左键：拖选或双击松开后，模拟 Ctrl+C 取到选中文字，再把剪贴板原样还回去。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.pid = os.getpid()

    def run(self):
        prev, down_pt, last_click = False, None, (0.0, None)
        dbl = user32.GetDoubleClickTime() / 1000
        while True:
            time.sleep(0.01)
            down = key_down(VK_LBUTTON)
            if down and not prev:
                down_pt = cursor_pos()
                self.app.events.put(('press', down_pt))
            elif prev and not down and down_pt:
                up = cursor_pos()
                trigger = False
                if abs(up[0] - down_pt[0]) + abs(up[1] - down_pt[1]) >= 8:
                    trigger, last_click = True, (0.0, None)
                else:
                    now = time.time()
                    lp = last_click[1]
                    trigger = now - last_click[0] <= dbl and lp and abs(lp[0] - up[0]) + abs(lp[1] - up[1]) < 8
                    last_click = (now, up)
                if trigger and self.app.cfg['select_convert']:
                    try:
                        self.handle(up)
                    except Exception:
                        pass
                    down = key_down(VK_LBUTTON)
            prev = down

    def target_ok(self, pt):
        target = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(*pt)), GA_ROOT)
        if not target or target != user32.GetAncestor(user32.GetForegroundWindow(), GA_ROOT):
            return False
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
        return pid.value != self.pid and class_name(target) not in SKIP_CLASSES and not is_fullscreen(target)

    def handle(self, pt):
        if not self.target_ok(pt):
            return
        # 稍等一下让目标程序完成选区；期间用户自己按了 Ctrl（多半是 Ctrl+C），就直接读他复制的内容
        user_copy = False
        end = time.time() + 0.12
        while time.time() < end:
            if key_down(VK_LBUTTON):
                return
            user_copy = user_copy or key_down(VK_CONTROL)
            time.sleep(0.01)
        # 按着 Shift/Alt/Win 时发 Ctrl+C 会变成别的快捷键（比如 Ctrl+Shift+C 打开开发者工具）
        end = time.time() + 0.8
        while any(key_down(k) for k in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)):
            if time.time() > end:
                return
            time.sleep(0.02)
        text = self.grab_selection(user_copy)
        if text:
            results = tp.parse(text, self.app.cfg['zone'])
            if results:
                self.app.events.put(('result', results, pt))

    @staticmethod
    def grab_selection(user_copy):
        if user_copy:  # 用户自己按的 Ctrl+C，是真复制，不用标记
            return SelectionWatcher._grab(False)
        CLIP_GUARD['busy'] = True
        own = True
        try:
            text, own = SelectionWatcher._grab(True)
            return text
        finally:
            # 我们自己发 Ctrl+C 再还原造成的剪贴板变化，告诉“复制提示”别弹
            CLIP_GUARD['seq'] = user32.GetClipboardSequenceNumber() if own else None
            CLIP_GUARD['busy'] = False

    @staticmethod
    def _grab(inject):
        """inject=True：自己发 Ctrl+C 并在读完后还原剪贴板。返回 (文字, 剪贴板是否是我们改的)。
        inject=False：等用户自己复制，直接读，返回文字。"""
        snap = None
        if inject:
            snap = clipboard_snapshot()
            if snap is None:
                return None, True  # 剪贴板被占用，备份不了就不动它
        seq = user32.GetClipboardSequenceNumber()
        if inject:
            send_ctrl_c()
        changed, user_pressed = False, False
        end = time.time() + (0.45 if inject else 0.6)
        while time.time() < end:
            time.sleep(0.015)
            user_pressed = user_pressed or (inject and key_down(VK_CONTROL))
            if user32.GetClipboardSequenceNumber() != seq:
                changed = True
                break
        if not changed:
            return (None, True) if inject else None
        time.sleep(0.05)
        text = clipboard_text()
        if not inject:
            return text
        user_pressed = user_pressed or key_down(VK_CONTROL)
        if not user_pressed:
            clipboard_restore(snap)
            return text, True
        return text, False  # 用户自己也按了 Ctrl+C：内容留给用户，算真复制


# ---------------------------------------------------------------- 设置
def detect_zone():
    off = datetime.now().astimezone().utcoffset()
    now = datetime.now(timezone.utc)
    for k in tp.ORDER:
        if now.astimezone(tp.get_tz(k)).utcoffset() == off:
            return k
    return 'cn'


def load_config():
    cfg = {'zone': None, 'show_others': True, 'select_convert': True, 'alpha': 0.93, 'x': None, 'y': None,
           'show_usage': True, 'usage_interval': 180,
           # 每个账号一个 Claude Code 配置目录（里面的 .credentials.json）。账号 2 用单独目录登录，不影响原来的登录
           'accounts': [{'dir': '~/.claude', 'email': None},
                        {'dir': '~/.claude-usage-2', 'email': None}]}
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if cfg['zone'] not in tp.ZONES:
        cfg['zone'] = detect_zone()
    return cfg


def autostart_cmd():
    exe = sys.executable
    if exe.lower().endswith('python.exe'):
        exe = exe[:-len('python.exe')] + 'pythonw.exe'
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- 换算弹窗
class Popup:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.configure(bg=ACCENT)
        self.body = tk.Frame(self.win, bg=POP_BG)
        self.body.pack(padx=1, pady=1)
        self.rect = None
        self.hide_job = None
        self.win.geometry('+-10000+-10000')
        self.win.update_idletasks()
        # 不抢焦点：弹出来时原程序里的选区保持高亮
        hwnd = int(self.win.wm_frame(), 16)
        style = user32.GetWindowLongPtrW(hwnd, -20)
        user32.SetWindowLongPtrW(hwnd, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
        self.hwnd = hwnd
        self.win.bind('<Enter>', lambda e: self.cancel_timer())
        self.win.bind('<Leave>', lambda e: self.schedule_hide(2500))

    def cancel_timer(self):
        if self.hide_job:
            self.app.root.after_cancel(self.hide_job)
            self.hide_job = None

    def schedule_hide(self, ms):
        self.cancel_timer()
        self.hide_job = self.app.root.after(ms, self.hide)

    def hide(self):
        self.cancel_timer()
        self.rect = None
        self.win.geometry('+-10000+-10000')

    def show(self, results, pt):
        s = self.app.px
        for w in self.body.winfo_children():
            w.destroy()
        row = 0
        for i, item in enumerate(results):
            if i:
                tk.Frame(self.body, bg=BORDER, height=1).grid(row=row, column=0, columnspan=4, sticky='ew', pady=s(4))
                row += 1
            hint = f'「{item["raw"]}」'
            if not item['zone_given']:
                hint += f'  未写时区，按{tp.zone_label(item["key"])}时间'
            if not item['date_given']:
                hint += '  ·  按今天'
            tk.Label(self.body, text=hint, bg=POP_BG, fg=SUB, font=(UI_FONT, 8)).grid(
                row=row, column=0, columnspan=4, sticky='w', padx=s(10), pady=(s(6), s(2)))
            row += 1
            for key, label, local, diff, is_src in tp.convert(item):
                bg = SRC_BG if is_src else POP_BG
                tag = {1: '次日', -1: '前一天'}.get(diff, f'{diff:+d}天' if diff else '')
                cells = [(label, FG if is_src else SUB, (UI_FONT, 9)),
                         (tp.fmt_date(local), FG, (UI_FONT, 9)),
                         (local.strftime('%H:%M'), ACCENT if not is_src else FG, (NUM_FONT, 13)),
                         (' '.join(x for x in (tp.abbr(local, key), tag) if x), SUB, (UI_FONT, 8))]
                for col, (text, fg, font) in enumerate(cells):
                    tk.Label(self.body, text=text, bg=bg, fg=fg, font=font, anchor='w').grid(
                        row=row, column=col, sticky='nsew', padx=0, ipadx=s(8), ipady=s(1))
                row += 1
        tk.Frame(self.body, bg=POP_BG, height=s(6)).grid(row=row, column=0)
        for w in [self.win] + list(self.body.winfo_children()):
            w.bind('<Button-1>', lambda e: self.hide())

        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        _, (l, t, r, b) = monitor_rects(*pt)
        x = min(max(pt[0] - s(16), l), r - w)
        y = pt[1] + s(22)
        if y + h > b:
            y = pt[1] - h - s(14)
        y = max(y, t)
        self.win.geometry(f'{w}x{h}+{x}+{y}')
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # 置顶，不移动不激活
        self.rect = (x, y, x + w, y + h)
        self.schedule_hide(8000)

    def contains(self, pt):
        return self.rect and self.rect[0] <= pt[0] < self.rect[2] and self.rect[1] <= pt[1] < self.rect[3]


# ---------------------------------------------------------------- Claude 额度
class UsageWorker(threading.Thread):
    """后台定时拉取每个账号的用量，结果丢进 app.events。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()

    def run(self):
        while True:
            waiting_login = False
            if self.app.cfg['show_usage']:
                for i, acc in enumerate(list(self.app.cfg['accounts'])):
                    try:
                        res = ('ok', cu.fetch(acc['dir'], want_email=not acc.get('email')))
                    except cu.NotLoggedIn:
                        res, waiting_login = ('nologin', None), True
                    except cu.AuthExpired:
                        res = ('expired', None)
                    except Exception as e:
                        res = ('error', str(e))
                    self.app.events.put(('usage', i, res))
            # 有账号还没登录时勤快点检查，登录完很快就能显示出来
            self.wake.wait(15 if waiting_login else self.app.cfg['usage_interval'])
            self.wake.clear()


def remain_color(rem):
    return OK_GREEN if rem >= 50 else WARN if rem >= 20 else BAD


class UsagePanel:
    """每个账号三行：账号名，5 小时额度，本周额度（剩余百分比 + 重置时间）。"""
    KINDS = (('five_hour', '5小时'), ('seven_day', '本周'))

    def __init__(self, app, parent):
        self.app = app
        s = app.px
        self.frame = tk.Frame(parent, bg=BG)
        tk.Frame(self.frame, bg=BORDER, height=1).grid(row=0, column=0, columnspan=4, sticky='ew', pady=(0, s(4)))
        self.state = {}  # 账号序号 -> ('ok'|'nologin'|'expired'|'error', 数据)
        self.data = {}   # 账号序号 -> 最近一次成功的数据
        self.accounts = []
        row = 1
        for i, _ in enumerate(app.cfg['accounts']):
            head = tk.Label(self.frame, bg=BG, fg=FG, font=(UI_FONT, 8), anchor='w')
            head.grid(row=row, column=0, columnspan=4, sticky='w', pady=(s(4) if i else 0, s(1)))
            row += 1
            lines = []
            for _, name in self.KINDS:
                kind = tk.Label(self.frame, text=name, bg=BG, fg=SUB, font=(UI_FONT, 8), anchor='w')
                bar = tk.Canvas(self.frame, width=s(58), height=s(6), bg=TRACK, highlightthickness=0, bd=0)
                fill = bar.create_rectangle(0, 0, 0, s(6), width=0, fill=OK_GREEN)
                pct = tk.Label(self.frame, bg=BG, fg=FG, font=(NUM_FONT, 9), anchor='e', width=4)
                reset = tk.Label(self.frame, bg=BG, fg=SUB, font=(UI_FONT, 8), anchor='w')
                kind.grid(row=row, column=0, sticky='w', padx=(0, s(6)))
                bar.grid(row=row, column=1, sticky='w')
                pct.grid(row=row, column=2, sticky='e', padx=(s(4), s(6)))
                reset.grid(row=row, column=3, sticky='w')
                lines.append((kind, bar, fill, pct, reset))
                row += 1
            self.accounts.append((head, lines))

    def widgets(self):
        return [self.frame] + self.frame.winfo_children()

    def account_of(self, widget):
        for i, (head, lines) in enumerate(self.accounts):
            if widget is head or any(widget in line for line in lines):
                return i
        return None

    def update(self, i, res):
        self.state[i] = res
        if res[0] == 'ok':
            self.data[i] = res[1]
            acc = self.app.cfg['accounts'][i]
            if res[1]['email'] and acc.get('email') != res[1]['email']:
                acc['email'] = res[1]['email']
                self.app.save()
        self.render()

    def render(self):
        s = self.app.px
        tz = tp.get_tz(self.app.cfg['zone'])
        now = datetime.now(timezone.utc)
        for i, (head, lines) in enumerate(self.accounts):
            acc = self.app.cfg['accounts'][i]
            name = (acc.get('email') or '').split('@')[0] or f'账号{i + 1}'
            st, _ = self.state.get(i, ('loading', None))
            d = self.data.get(i)
            plan = f' · {d["plan"]}' if d and d['plan'] else ''
            note = {'loading': '  读取中…', 'nologin': '  未登录 · 点这里登录', 'expired': '  登录失效 · 点这里重新登录',
                    'error': '  离线' + (f'（{datetime.fromtimestamp(d["time"]):%H:%M}）' if d else '')}.get(st, '')
            head.configure(text=f'{name}{plan}{note}', fg=WARN if st in ('nologin', 'expired') else FG,
                           cursor='hand2' if st in ('nologin', 'expired') else '')
            show = d if st in ('ok', 'error') else None
            for (key, _), (kind, bar, fill, pct, reset) in zip(self.KINDS, lines):
                w = show and show.get(key)
                if not w:
                    bar.coords(fill, 0, 0, 0, s(6))
                    pct.configure(text='—', fg=SUB)
                    reset.configure(text='')
                    continue
                rem = max(0.0, 100 - w['used'])
                color = remain_color(rem)
                bar.coords(fill, 0, 0, round(s(58) * rem / 100), s(6))
                bar.itemconfigure(fill, fill=color)
                pct.configure(text=f'{rem:.0f}%', fg=color)
                reset.configure(text=self.reset_text(key, w['resets_at'], now, tz))

    @staticmethod
    def reset_text(key, at, now, tz):
        if at is None:
            return '未开始计时' if key == 'five_hour' else ''
        if key == 'five_hour':
            mins = max(0, int((at - now).total_seconds() // 60) + 1)
            h, m = divmod(mins, 60)
            return f'{h}时{m:02d}分后重置' if h else f'{m}分后重置'
        local = at.astimezone(tz)
        return f'{local.month}/{local.day} 周{tp.WEEK_CN[local.weekday()]} {local:%H:%M} 重置'


# ---------------------------------------------------------------- 主窗口
class App:
    def __init__(self):
        self.cfg = load_config()
        self.events = queue.Queue()
        self.root = root = tk.Tk()
        root.title('时区悬浮窗')
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.attributes('-alpha', self.cfg['alpha'])
        root.configure(bg=BORDER)
        self.scale = root.winfo_fpixels('1i') / 96

        box = tk.Frame(root, bg=BG)
        box.pack(padx=1, pady=1)
        s = self.px
        top = tk.Frame(box, bg=BG)
        top.pack(fill='x', padx=s(12), pady=(s(8), 0))
        self.lbl_zone = tk.Label(top, bg=BG, fg=SUB, font=(UI_FONT, 9))
        self.lbl_zone.pack(side='left')
        self.lbl_flag = tk.Label(top, bg=BG, font=(UI_FONT, 8), cursor='hand2')
        self.lbl_flag.pack(side='right', padx=(s(14), 0))
        self.lbl_time = tk.Label(box, bg=BG, fg=FG, font=(NUM_FONT, 24))
        self.lbl_time.pack(anchor='w', padx=s(10))
        self.lbl_date = tk.Label(box, bg=BG, fg=SUB, font=(UI_FONT, 9))
        self.lbl_date.pack(anchor='w', padx=s(12))
        self.others = tk.Frame(box, bg=BG)
        self.other_labels = []
        for _ in range(2):
            lbl = tk.Label(self.others, bg=BG, fg=SUB, font=(UI_FONT, 9), anchor='w')
            lbl.pack(fill='x')
            self.other_labels.append(lbl)
        self.pad_bottom = tk.Frame(box, bg=BG, height=s(8))
        self.pad_bottom.pack(fill='x')
        self.usage = UsagePanel(self, box)
        self.apply_others()
        self.apply_usage()

        self.build_menu()
        self.drag = None
        for w in [root, box, top, self.others, self.pad_bottom, *top.winfo_children(), *box.winfo_children(),
                  *self.other_labels, *self.usage.widgets()]:
            w.bind('<ButtonPress-1>', self.on_press)
            w.bind('<B1-Motion>', self.on_motion)
            w.bind('<ButtonRelease-1>', self.on_release)
            w.bind('<Button-3>', self.on_menu)

        root.update_idletasks()
        x, y = self.cfg['x'], self.cfg['y']
        if x is None or not self.on_screen(x, y):
            _, (l, t, r, b) = monitor_rects(*cursor_pos())
            x, y = r - root.winfo_reqwidth() - s(24), t + s(24)
        root.geometry(f'+{x}+{y}')

        self.popup = Popup(self)
        self.refresh_flag()
        self.tick()
        self.poll()
        self.keep_on_top()
        SelectionWatcher(self).start()
        self.usage_worker = UsageWorker(self)
        self.usage_worker.start()

    def px(self, v):
        return int(round(v * self.scale))

    @staticmethod
    def on_screen(x, y):
        (l, t, r, b), _ = monitor_rects(x + 20, y + 20)
        return l <= x + 20 < r and t <= y + 20 < b

    # -- 菜单
    def build_menu(self):
        self.zone_var = tk.StringVar(value=self.cfg['zone'])
        self.others_var = tk.BooleanVar(value=self.cfg['show_others'])
        self.select_var = tk.BooleanVar(value=self.cfg['select_convert'])
        self.alpha_var = tk.DoubleVar(value=self.cfg['alpha'])
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        self.usage_var = tk.BooleanVar(value=self.cfg['show_usage'])
        m = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 9))
        for k in tp.ORDER:
            m.add_radiobutton(label=f'{tp.ZONES[k][0]}时间', value=k, variable=self.zone_var,
                              command=lambda: self.set_zone(self.zone_var.get()))
        m.add_separator()
        m.add_checkbutton(label='显示其他时区', variable=self.others_var, command=self.toggle_others)
        m.add_checkbutton(label='划词换算', variable=self.select_var, command=self.toggle_select)
        m.add_checkbutton(label='显示 Claude 额度', variable=self.usage_var, command=self.toggle_usage)
        m.add_command(label='立即刷新额度', command=self.refresh_usage)
        login = tk.Menu(m, tearoff=0, font=(UI_FONT, 9))
        for i, acc in enumerate(self.cfg['accounts']):
            login.add_command(label=f'账号{i + 1}（{acc["dir"]}）', command=lambda i=i: self.login(i))
        m.add_cascade(label='登录 Claude 账号', menu=login)
        sub = tk.Menu(m, tearoff=0, font=(UI_FONT, 9))
        for a in (1.0, 0.93, 0.85, 0.75, 0.6):
            sub.add_radiobutton(label=f'{int(a * 100)}%', value=a, variable=self.alpha_var,
                                command=lambda: self.set_alpha(self.alpha_var.get()))
        m.add_cascade(label='不透明度', menu=sub)
        m.add_checkbutton(label='开机自启', variable=self.auto_var, command=lambda: set_autostart(self.auto_var.get()))
        m.add_separator()
        m.add_command(label='退出', command=self.quit)
        self.menu = m

    def on_menu(self, e):
        self.auto_var.set(autostart_enabled())
        self.menu.tk_popup(e.x_root, e.y_root)

    def set_zone(self, k):
        self.cfg['zone'] = k
        self.zone_var.set(k)
        self.save()
        self.tick(reschedule=False)

    def toggle_others(self):
        self.cfg['show_others'] = self.others_var.get()
        self.apply_others()
        self.save()

    def apply_others(self):
        self.others.pack_forget()
        if self.cfg['show_others']:
            self.others.pack(fill='x', padx=self.px(12), pady=(self.px(6), 0), before=self.pad_bottom)

    def toggle_usage(self):
        self.cfg['show_usage'] = self.usage_var.get()
        self.apply_usage()
        self.save()
        if self.cfg['show_usage']:
            self.refresh_usage()

    def apply_usage(self):
        self.usage.frame.pack_forget()
        if self.cfg['show_usage']:
            self.usage.frame.pack(fill='x', padx=self.px(12), pady=(self.px(8), 0), before=self.pad_bottom)

    def refresh_usage(self):
        self.usage_worker.wake.set()

    def login(self, i):
        acc = self.cfg['accounts'][i]
        try:
            cu.open_login(acc['dir'], acc.get('login_email') or acc.get('email'))
        except Exception:
            pass

    def toggle_select(self):
        self.cfg['select_convert'] = self.select_var.get()
        self.refresh_flag()
        self.save()

    def refresh_flag(self):
        on = self.cfg['select_convert']
        self.lbl_flag.configure(text='● 划词' if on else '○ 划词', fg=OK_GREEN if on else SUB)

    def set_alpha(self, a):
        self.cfg['alpha'] = a
        self.root.attributes('-alpha', a)
        self.save()

    # -- 拖动 / 单击
    def on_press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

    def on_motion(self, e):
        if not self.drag:
            return
        x0, y0, wx, wy, moved = self.drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if moved or abs(dx) + abs(dy) > 3:
            self.drag = (x0, y0, wx, wy, True)
            self.root.geometry(f'+{wx + dx}+{wy + dy}')

    def on_release(self, e):
        if not self.drag:
            return
        moved = self.drag[4]
        self.drag = None
        if moved:
            self.cfg['x'], self.cfg['y'] = self.root.winfo_x(), self.root.winfo_y()
            self.save()
        elif e.widget is self.lbl_flag:
            self.select_var.set(not self.select_var.get())
            self.toggle_select()
        elif e.widget in self.usage.widgets():
            # 点额度区：没登录就打开登录，否则立即刷新
            i = self.usage.account_of(e.widget)
            if i is not None and self.usage.state.get(i, ('',))[0] in ('nologin', 'expired'):
                self.login(i)
            else:
                self.refresh_usage()
        else:
            i = tp.ORDER.index(self.cfg['zone'])
            self.set_zone(tp.ORDER[(i + 1) % len(tp.ORDER)])

    # -- 刷新
    def tick(self, reschedule=True):
        now = datetime.now(timezone.utc)
        key = self.cfg['zone']
        local = now.astimezone(tp.get_tz(key))
        self.lbl_zone.configure(text=f'{tp.ZONES[key][0]} · {tp.abbr(local, key)}')
        self.lbl_time.configure(text=local.strftime('%H:%M:%S'))
        self.lbl_date.configure(text=tp.fmt_date(local))
        for lbl, k in zip(self.other_labels, [k for k in tp.ORDER if k != key]):
            o = now.astimezone(tp.get_tz(k))
            diff = (o.date() - local.date()).days
            tag = {1: '  次日', -1: '  前一天'}.get(diff, '')
            lbl.configure(text=f'{tp.ZONES[k][0]}  {o:%H:%M}  周{tp.WEEK_CN[o.weekday()]}{tag}')
        if self.cfg['show_usage']:
            self.usage.render()
        if reschedule:
            self.root.after(1000 - now.microsecond // 1000 + 10, self.tick)

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == 'press':
                    if self.popup.rect and not self.popup.contains(ev[1]):
                        self.popup.hide()
                elif ev[0] == 'result':
                    self.popup.show(ev[1], ev[2])
                elif ev[0] == 'usage':
                    self.usage.update(ev[1], ev[2])
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def keep_on_top(self):
        # 有些全屏/置顶程序会把悬浮窗压下去，定期重新置顶
        self.root.attributes('-topmost', True)
        self.root.after(15000, self.keep_on_top)

    def save(self):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def quit(self):
        self.save()
        self.root.destroy()


def main():
    kernel32.CreateMutexW(None, False, 'TimezoneFloat_SingleInstance')
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：已经开着一个了
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        user32.SetProcessDPIAware()
    App().root.mainloop()


if __name__ == '__main__':
    main()
