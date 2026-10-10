# -*- coding: utf-8 -*-
"""屏幕顶部居中的“刘海”状态条。

平时缩成一个黑色小条（显示北京/洛杉矶时间和各账号额度小圆点）；
鼠标移到上面展开：北京、洛杉矶时间，两个 Claude 账号、Claude 合计、两个 Codex 账号的额度圆环；
鼠标停在某个圆环上，下方弹出详情卡片。鼠标移开自动缩回。

左键点圆环：立即刷新（没登录的账号则打开登录）；右键：菜单。
划词换算功能沿用 timezone_float。
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime, timezone

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import claude_usage as cu
import clip_watch as cw
import codex_usage as cx
import timeparse as tp
import timezone_float as tf
import token_stats as ts
import window_hang as wh

user32, kernel32 = tf.user32, tf.kernel32
gdi32 = ctypes.WinDLL('gdi32')
winmm = ctypes.WinDLL('winmm')

# ---------------------------------------------------------------- 外观
BLACK = (0, 0, 0, 255)
CARD_BG = (22, 22, 24, 255)
CARD_EDGE = (48, 48, 52, 255)
WHITE = (245, 245, 247, 255)
GRAY = (142, 142, 147, 255)
DIM = (99, 99, 104, 255)
TRACK = (44, 44, 48, 255)
DIVIDER = (40, 40, 44, 255)
GREEN, AMBER, RED = (52, 199, 89, 255), (255, 159, 10, 255), (255, 69, 58, 255)
CLAUDE = (217, 119, 87, 255)
DAY, NIGHT = (255, 179, 64, 255), (142, 155, 255, 255)

TAB_W, TAB_H, TAB_R = 210, 26, 11
EAR = 8
ITEM_W, PAD_X, GROUP_GAP = 80, 18, 16
PANEL_H, PANEL_R = 118, 22
CARD_W, CARD_GAP = 304, 12
SHADOW = 18
SS = 3  # 超采样倍数，圆环和外形抗锯齿
EXPAND_TIME, COLLAPSE_TIME = 0.14, 0.12  # 秒
ENTER_DELAY, LEAVE_DELAY = 0.03, 0.2     # 鼠标停多久展开 / 离开多久缩回
CLIP_IN_TIME, CLIP_OUT_TIME = 0.34, 0.2  # 复制内容 / 提醒弹出、收起的时长
DROP_TIME = 0.2                          # 拖窗口靠近灵动岛时变形的时长
REST_SHOW = 2.0                          # 休息提醒显示几秒
REST_IDLE_RESET = 5 * 60                 # 离开电脑这么久算已经休息过，重新计时
WARM = (255, 204, 128, 255)
ACCENT = (10, 132, 255, 255)              # 框住复制内容的蓝框

FONT_DIR = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
_fonts = {}


def font(kind, size):
    names = {'cn': ['msyh.ttc'], 'cnb': ['msyhbd.ttc', 'msyh.ttc'], 'num': ['seguisb.ttf', 'segoeuib.ttf', 'msyhbd.ttc'],
             'sym': ['seguisym.ttf', 'msyh.ttc'], 'mono': ['consolab.ttf', 'msyhbd.ttc']}[kind]
    key = (kind, size)
    if key not in _fonts:
        for n in names:
            try:
                _fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, n), size)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def remain_color(rem):
    return GREEN if rem >= 50 else AMBER if rem >= 20 else RED


def dim(c, k=0.55):
    return tuple(int(v * k) for v in c[:3]) + (255,)


# ---------------------------------------------------------------- 绘图小件
def ring(img, cx, cy, r, width, frac, color, track=TRACK):
    size = int(2 * r + width + 4)
    big = Image.new('RGBA', (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    c, R, w = size * SS / 2, r * SS, width * SS
    box = [c - R - w / 2, c - R - w / 2, c + R + w / 2, c + R + w / 2]
    d.arc(box, 0, 360, fill=track, width=max(1, round(w)))
    frac = max(0.0, min(1.0, frac))
    if frac >= 0.999:
        d.arc(box, 0, 360, fill=color, width=max(1, round(w)))
    elif frac > 0.004:
        a0, a1 = -90, -90 + 360 * frac
        d.arc(box, a0, a1, fill=color, width=max(1, round(w)))
        for a in (a0, a1):  # 圆头
            x, y = c + R * math.cos(math.radians(a)), c + R * math.sin(math.radians(a))
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)
    img.alpha_composite(big.resize((size, size), Image.LANCZOS), (round(cx - size / 2), round(cy - size / 2)))


def _glyph(img, cx, cy, r, draw_fn):
    size = int(2 * r + 4)
    big = Image.new('RGBA', (size * SS, size * SS), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(big), size * SS / 2, r * SS)
    img.alpha_composite(big.resize((size, size), Image.LANCZOS), (round(cx - size / 2), round(cy - size / 2)))


def claude_star(img, cx, cy, r, color=CLAUDE):
    def f(d, c, R):
        w = R * 0.24
        for k in range(10):
            a = math.radians(k * 36 - 90)
            L = R * (1.0 if k % 2 == 0 else 0.78)
            x, y = c + L * math.cos(a), c + L * math.sin(a)
            d.line([c, c, x, y], fill=color, width=round(w))
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)
        d.ellipse([c - w * 0.9, c - w * 0.9, c + w * 0.9, c + w * 0.9], fill=color)
    _glyph(img, cx, cy, r, f)


def clock_icon(img, cx, cy, r, t, color):
    def f(d, c, R):
        w = R * 0.16
        d.ellipse([c - R, c - R, c + R, c + R], outline=color, width=round(w))
        ha = math.radians((t.hour % 12 + t.minute / 60) * 30 - 90)
        ma = math.radians(t.minute * 6 - 90)
        for a, L, ww in ((ha, 0.5, w * 1.2), (ma, 0.75, w)):
            x, y = c + R * L * math.cos(a), c + R * L * math.sin(a)
            d.line([c, c, x, y], fill=color, width=round(ww))
            d.ellipse([x - ww / 2, y - ww / 2, x + ww / 2, y + ww / 2], fill=color)
        d.ellipse([c - w, c - w, c + w, c + w], fill=color)
    _glyph(img, cx, cy, r, f)


def bar(d, x0, y0, x1, h, frac, color, track=TRACK):
    d.rounded_rectangle([x0, y0, x1, y0 + h], radius=h / 2, fill=track)
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        d.rounded_rectangle([x0, y0, max(x0 + h, x0 + (x1 - x0) * frac), y0 + h], radius=h / 2, fill=color)


_sprites = {}


def _quarter(r):
    """右下角圆角用的 1/4 圆（圆心在左上角），抗锯齿，按半径缓存。"""
    key = ('q', r)
    if key not in _sprites:
        big = Image.new('L', (r * SS, r * SS), 0)
        ImageDraw.Draw(big).ellipse([-r * SS, -r * SS, r * SS - 1, r * SS - 1], fill=255)
        _sprites[key] = big.resize((r, r), Image.LANCZOS)
    return _sprites[key]


def _ear(e):
    """左上角的反向小圆角：方块减掉圆心在左下角的 1/4 圆。"""
    key = ('e', e)
    if key not in _sprites:
        big = Image.new('L', (e * SS, e * SS), 255)
        ImageDraw.Draw(big).ellipse([-e * SS, 0, e * SS - 1, 2 * e * SS - 1], fill=0)
        _sprites[key] = big.resize((e, e), Image.LANCZOS)
    return _sprites[key]


def fast_notch_mask(w, h, r, e):
    """刘海外形：上边贴着屏幕顶，下面两个圆角，左右顶上两个反向小圆角。
    用缓存好的抗锯齿圆角拼出来，任意尺寸都不到 1 毫秒，动画每帧都能重算。"""
    w, h, e = max(4, int(round(w))), max(4, int(round(h))), int(e)
    r = int(max(1, min(round(r), h, w // 2)))
    m = Image.new('L', (w + 2 * e, h), 0)
    m.paste(255, (e, 0, e + w, h - r))
    m.paste(255, (e + r, h - r, e + w - r, h))
    q = _quarter(r)
    m.paste(q, (e + w - r, h - r))
    m.paste(q.transpose(Image.FLIP_LEFT_RIGHT), (e, h - r))
    if e:
        ear = _ear(e)
        m.paste(ear, (0, 0))
        m.paste(ear.transpose(Image.FLIP_LEFT_RIGHT), (e + w, 0))
    return m


def fast_shadow(mask, blur, strength):
    """在 1/4 分辨率上做模糊再放大：阴影本来就是糊的，看不出区别，快十几倍。"""
    k = 4
    pad = max(2, blur * 2 // k)
    small = mask.resize((max(1, mask.width // k), max(1, mask.height // k)), Image.BILINEAR)
    m = Image.new('L', (small.width + 2 * pad, small.height + 2 * pad), 0)
    m.paste(small, (pad, pad))
    m = m.filter(ImageFilter.GaussianBlur(max(1, blur / k))).point(lambda v: int(v * strength))
    m = m.resize((m.width * k, m.height * k), Image.BILINEAR)
    sh = Image.new('RGBA', m.size, (0, 0, 0, 0))
    sh.putalpha(m)
    return sh, pad * k


def mug_icon(img, cx, cy, r, color=WARM):
    """休息提醒用的小咖啡杯：杯身 + 把手 + 三缕热气。"""
    def f(d, c, R):
        w = R * 0.13
        top, bottom = c - R * 0.15, c + R * 0.75
        d.rounded_rectangle([c - R * 0.62, top, c + R * 0.38, bottom], radius=R * 0.22, fill=color)
        d.arc([c + R * 0.12, top + R * 0.12, c + R * 0.82, top + R * 0.62], -90, 90, fill=color, width=round(w))
        for dx in (-0.4, -0.12, 0.16):
            x = c + R * dx
            d.arc([x - R * 0.12, c - R * 0.95, x + R * 0.12, c - R * 0.6], 90, 270, fill=color, width=round(w * 0.8))
            d.arc([x - R * 0.12, c - R * 0.62, x + R * 0.12, c - R * 0.3], -90, 90, fill=color, width=round(w * 0.8))
    _glyph(img, cx, cy, r, f)


def build_rest_layer(w, h, minutes, P):
    """休息提醒：和用量面板一样大。"""
    img = Image.new('RGBA', (int(w), int(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mug_icon(img, P(70), h / 2 - P(4), P(30))
    hh, mm = divmod(int(minutes), 60)
    used = (f'{hh} 小时' if hh else '') + (f' {mm} 分钟' if mm else '')
    d.text((P(122), h / 2 - P(14)), '休息一下', font=font('cnb', P(28)), fill=WHITE, anchor='lm')
    d.text((P(124), h / 2 + P(22)), f'已经连续用电脑 {used.strip()}了 · 起来走走，看看远处', font=font('cn', P(13)),
           fill=GRAY, anchor='lm')
    return img


def build_toast_layer(text, P, color=AMBER):
    f = font('cn', P(12.5))
    w, h = int(f.getlength(text) + P(44)), P(42)
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((w / 2, h / 2), text, font=f, fill=color, anchor='mm')
    return img


def build_drop_layer(kind, n, P):
    """拖窗口时灵动岛的样子：靠近时“拖到这里悬挂”，进入范围时变大发蓝光“松手悬挂”，满了提示最多 3 个。"""
    hot = kind == 'hot'
    w, h = (P(270), P(50)) if hot else (P(240), P(42))
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    if hot:
        frame = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rounded_rectangle([P(6), P(6), w - P(6), h - P(6)], radius=(h - P(12)) // 2,
                                                outline=ACCENT, width=max(2, P(2)))
        img.alpha_composite(frame.filter(ImageFilter.GaussianBlur(P(4))))
        img.alpha_composite(frame)
    d = ImageDraw.Draw(img)
    color = {'near': GRAY, 'hot': WHITE, 'full': AMBER}[kind]
    text = {'near': '拖到这里悬挂', 'hot': f'松手悬挂  {n + 1}/{wh.MAX_HANG}', 'full': f'最多悬挂 {wh.MAX_HANG} 个'}[kind]
    # 小图标：一个往下挂的箭头
    cx, cy, lw = P(30), h / 2, max(2, P(2))
    d.line([cx - P(8), cy - P(8), cx + P(8), cy - P(8)], fill=color, width=lw)
    d.line([cx, cy - P(4), cx, cy + P(7)], fill=color, width=lw)
    d.line([cx - P(5), cy + P(2), cx, cy + P(7), cx + P(5), cy + P(2)], fill=color, width=lw, joint='curve')
    d.text((P(48), h / 2), text, font=font('cnb' if hot else 'cn', P(13)), fill=color, anchor='lm')
    return img


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('dwTime', wt.DWORD)]


def idle_seconds():
    lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000


def card_mask(w, h, r, tip_x, tip_h):
    W, H = int(w), int(h + tip_h)
    big = Image.new('L', (W * SS, H * SS), 0)
    d = ImageDraw.Draw(big)
    s = SS
    d.rounded_rectangle([0, tip_h * s, w * s - 1, (h + tip_h) * s - 1], radius=r * s, fill=255)
    d.polygon([((tip_x - tip_h * 1.15) * s, tip_h * s + 1), (tip_x * s, 0), ((tip_x + tip_h * 1.15) * s, tip_h * s + 1)],
              fill=255)
    return big.resize((W, H), Image.LANCZOS)


def shadow_of(mask, blur, strength):
    pad = blur * 2
    m = Image.new('L', (mask.width + pad * 2, mask.height + pad * 2), 0)
    m.paste(mask, (pad, pad))
    m = m.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * strength))
    sh = Image.new('RGBA', m.size, (0, 0, 0, 0))
    sh.putalpha(m)
    return sh, pad


# ---------------------------------------------------------------- 复制内容
def _wrap(text, f, width, max_lines):
    """按像素宽度折行（中文逐字），最多 max_lines 行，放不下时最后一行加省略号。"""
    text = text.replace('\r\n', '\n').replace('\t', '    ').strip()
    cut = len(text) > 800
    lines = []
    for para in text[:800].split('\n'):
        cur = ''
        for ch in para:
            if cur and f.getlength(cur + ch) > width:
                head, sp, word = cur.rpartition(' ')
                if ch.isascii() and ch.isalnum() and sp and word.isascii() and head:
                    lines.append(head)  # 英文单词整个挪到下一行，不从中间断开
                    cur = word + ch
                else:
                    lines.append(cur)
                    cur = '' if ch == ' ' else ch  # 折行处的空格不留到下一行开头
            else:
                cur += ch
        lines.append(cur)
        if len(lines) > max_lines:
            break
    cut = cut or len(lines) > max_lines
    lines = lines[:max_lines]
    if cut:
        last = lines[-1]
        while last and f.getlength(last + '…') > width:
            last = last[:-1]
        lines[-1] = last + '…'
    return lines


def _rounded(img, radius):
    m = Image.new('L', (img.width * SS, img.height * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, m.width - 1, m.height - 1], radius=radius * SS, fill=255)
    out = img.copy()
    out.putalpha(Image.composite(img.getchannel('A'), Image.new('L', img.size, 0),
                                 m.resize(img.size, Image.LANCZOS)))
    return out


def build_clip_layer(clip, P):
    """把复制的内容画成灵动岛里的一块：标题 + 蓝框框住的预览 + 收起提示。返回 RGBA 图（就是灵动岛的大小）。"""
    pad, inset, bw = P(16), P(5), max(2, P(2))
    kind = clip['kind']
    thumb, body_lines, info = None, None, ''
    if kind in ('image', 'files') and clip.get('image') is not None:
        im = clip['image']
        k = min(P(300) / im.width, P(170) / im.height, 1.0 if im.width > P(60) else 3.0)
        thumb = _rounded(im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS), P(7))
        info = '{}×{}'.format(*clip['size'])
    if kind == 'image':
        title = '已复制图片'
    elif kind == 'text':
        n = len(clip['text'].strip())
        title, info = '已复制文本', f'{n} 字'
        body_lines = _wrap(clip['text'], font('cn', P(12.5)), P(320), 4)
    elif kind == 'files':
        files = clip['files']
        title = f'已复制 {len(files)} 个文件' if len(files) > 1 else '已复制文件'
        if thumb is None:
            names = [os.path.basename(f.rstrip('\\')) or f for f in files[:3]]
            body_lines = [('▸ ' + n) for n in names] + ([f'… 等 {len(files)} 个'] if len(files) > 3 else [])
        else:
            info = os.path.basename(files[0])
    else:
        title, body_lines = '已复制', ['内容已隐藏（来源要求不在屏幕上显示）']

    fb = font('cn', P(12.5))
    if thumb is not None:
        box_w, box_h = thumb.width + 2 * (inset + bw), thumb.height + 2 * (inset + bw)
    else:
        tw = max([fb.getlength(t) for t in body_lines] + [P(120)])
        box_w = int(min(tw, P(320)) + 2 * (P(10) + bw))
        box_h = int(len(body_lines) * P(19) + 2 * (P(8) + bw))
    ft, fi = font('cnb', P(12)), font('cn', P(10.5))
    info_w = fi.getlength(info) if info else 0
    if info_w > P(200):
        while info and fi.getlength(info + '…') > P(200):
            info = info[:-1]
        info += '…'
        info_w = fi.getlength(info)
    head_w = P(22) + ft.getlength(title) + P(16) + info_w
    w = int(max(box_w, head_w, P(200)) + 2 * pad)
    head_y, box_y = P(17), P(31)
    h = int(box_y + box_h + P(30))
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 标题行：蓝点 + 标题 + 右侧尺寸/字数
    d.ellipse([pad, head_y - P(4), pad + P(8), head_y + P(4)], fill=ACCENT)
    d.text((pad + P(16), head_y), title, font=ft, fill=WHITE, anchor='lm')
    if info:
        d.text((w - pad, head_y), info, font=fi, fill=GRAY, anchor='rm')
    # 蓝框（带一点光晕）
    bx = (w - box_w) // 2
    frame = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle([bx, box_y, bx + box_w - 1, box_y + box_h - 1], radius=P(11),
                                            outline=ACCENT, width=bw, fill=(24, 24, 27, 255) if thumb is None else None)
    glow = frame.filter(ImageFilter.GaussianBlur(P(5)))
    img.alpha_composite(glow)
    img.alpha_composite(frame)
    if thumb is not None:
        img.alpha_composite(thumb, (bx + inset + bw, box_y + inset + bw))
    else:
        y = box_y + bw + P(8) + P(9.5)
        for line in body_lines:
            d.text((bx + bw + P(10), y), line, font=fb, fill=WHITE if kind != 'hidden' else GRAY, anchor='lm')
            y += P(19)
    d.text((w / 2, box_y + box_h + P(15)), '粘贴或点这里收起', font=font('cn', P(10)), fill=DIM, anchor='mm')
    return img


# ---------------------------------------------------------------- 分层窗口（逐像素透明）
class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class LayeredSurface:
    """把 PIL 图贴到分层窗口上。位图按尺寸复用，避免每帧新建。"""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        self.size = None
        self.hbmp = self.old = None
        self.bits = ctypes.c_void_p()

    def _ensure(self, w, h):
        if self.size == (w, h):
            return
        if self.hbmp:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.hbmp)
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        self.hbmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.mem, self.hbmp)
        self.size = (w, h)

    def update(self, img, x, y):
        """img 是 RGBA 图，或者已经排好的 (BGRA 字节, 宽, 高)。"""
        if isinstance(img, tuple):
            data, w, h = img
        else:
            w, h = img.size
            data = img.tobytes('raw', 'BGRa')  # Pillow 直接给出预乘 alpha 的 BGRA
        self._ensure(w, h)
        ctypes.memmove(self.bits, data, len(data))
        user32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                   self.mem, ctypes.byref(wt.POINT(0, 0)), 0,
                                   ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)


# ---------------------------------------------------------------- 数据
class Worker(threading.Thread):
    """后台定时拉取所有 Claude / Codex 账号的用量。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()

    @staticmethod
    def _get(fn):
        try:
            return 'ok', fn()
        except cu.NotLoggedIn:
            return 'nologin', None
        except cu.AuthExpired:
            return 'expired', None
        except Exception as e:
            return 'error', str(e)

    def run(self):
        while True:
            waiting = False
            for i, acc in enumerate(list(self.app.cfg['accounts'])):
                res = self._get(lambda: cu.fetch(acc['dir'], want_email=not (acc.get('email') and acc.get('account_uuid'))))
                waiting |= res[0] == 'nologin'
                self.app.events.put(('usage', 'claude', i, res))
            for i, acc in enumerate(list(self.app.cfg['codex_accounts'])):
                res = self._get(lambda: cx.fetch(acc['path']))
                waiting |= res[0] == 'nologin'
                self.app.events.put(('usage', 'codex', i, res))
            self.wake.wait(15 if waiting else self.app.cfg['usage_interval'])
            self.wake.clear()


class TokenWorker(threading.Thread):
    """统计本机日志里的 token：扫描放在一个低优先级子进程里（不抢界面），首次全量几秒，之后每 2 分钟只读新增部分。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()
        self.proc = None

    def _ensure_proc(self):
        if self.proc and self.proc.poll() is None:
            return
        if getattr(sys, 'frozen', False):  # 打包成 exe：用自己加参数启动统计进程
            args = [sys.executable, '--token-stats']
        else:
            exe = sys.executable
            if exe.lower().endswith('pythonw.exe'):  # 子进程要用 stdout，用带控制台的 python（不弹窗口）
                exe = exe[:-len('pythonw.exe')] + 'python.exe'
            args = [exe, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token_stats.py')]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00004000)  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS

    def run(self):
        time.sleep(1.5)  # 让界面先起来
        while True:
            try:
                self._ensure_proc()
                req = {'claude': self.app.cfg['accounts'], 'codex': self.app.cfg['codex_accounts']}
                self.proc.stdin.write((json.dumps(req) + '\n').encode())
                self.proc.stdin.flush()
                out = json.loads(self.proc.stdout.readline() or b'{}')
                if 'error' not in out:
                    res = {(k.split(':')[0], int(k.split(':')[1])): v for k, v in out.items()}
                    self.app.events.put(('tokens', res))
            except Exception:
                self.proc = None
            self.wake.wait(120)
            self.wake.clear()


# ---------------------------------------------------------------- 刘海
class Notch:
    def __init__(self, app):
        self.app = app
        P = self.P = app.px
        self.items = [('time', 'cn'), ('time', 'la')]
        breaks = [len(self.items)]
        self.items += [('claude', i) for i in range(len(app.cfg['accounts']))]
        if len(app.cfg['accounts']) > 1:
            self.items.append(('ctotal', None))
        breaks.append(len(self.items))
        self.items += [('codex', i) for i in range(len(app.cfg['codex_accounts']))]
        # 每个圆环的中心 x（相对面板左边）
        self.centers, self.dividers = [], []
        x = P(PAD_X)
        for i in range(len(self.items)):
            if i in breaks and i:
                self.dividers.append(x + P(GROUP_GAP) / 2)
                x += P(GROUP_GAP)
            self.centers.append(x + P(ITEM_W) / 2)
            x += P(ITEM_W)
        self.panel_w = x + P(PAD_X)
        self.panel_h = P(PANEL_H)
        self.tab_w, self.tab_h = P(TAB_W), P(TAB_H)
        self.ear = P(EAR)
        # 画布要装得下最大的复制提示（2.5 倍时约 370×260 的 2.5 倍）
        self.W = int(max(self.panel_w, P(CARD_W), P(370 * 2.5)) + 2 * self.ear + 2 * P(SHADOW))
        self.H = int(max(self.panel_h + P(CARD_GAP) + P(560), P(260 * 2.5)) + P(SHADOW))

        mon = (user32.MonitorFromPoint(wt.POINT(0, 0), 1))  # 主屏
        info = tf.MONITORINFO(cbSize=ctypes.sizeof(tf.MONITORINFO))
        user32.GetMonitorInfoW(mon, ctypes.byref(info))
        m = info.rcMonitor
        self.mon = (m.left, m.top, m.right, m.bottom)
        self.x = (m.left + m.right) // 2 - self.W // 2
        self.y = m.top

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.geometry(f'{self.W}x{self.H}+{self.x}+{self.y}')
        self.win.update_idletasks()
        self.hwnd = int(self.win.wm_frame(), 16)
        ex = user32.GetWindowLongPtrW(self.hwnd, -20)
        user32.SetWindowLongPtrW(self.hwnd, -20, ex | 0x00080000 | 0x00000080 | 0x08000000)  # LAYERED|TOOL|NOACTIVATE
        self.win.bind('<ButtonRelease-1>', self.on_click)
        self.win.bind('<Button-3>', app.on_menu)

        self.hover = None      # 当前详情卡片对应的圆环序号
        self.enter_since = None
        self.leave_since = None
        self.hidden = False
        self.card_rect = None
        self._panel_cache = (None, None)
        self._tab_cache = (None, None)
        self._card_cache = {}
        self._chrome_cache = {}
        self._shadow_cache = {}
        self._black_cache = {}
        self._settled_cache = {}
        # 灵动岛在显示什么：tab 小条 / panel 用量面板 / clip 复制内容 / alert 提醒 / drop 拖窗口悬挂。
        # 所有切换都走同一个变形动画：外形从当前大小变到目标大小，旧内容淡出、新内容淡入。
        self.state, self.content = 'tab', None
        self.cur_dims = self.tab_dims()
        self.anim = None
        self.target_key = ('tab', 0)
        self.t = 0.0             # 1 = 用量面板已完全展开（这时才能点圆环、显示卡片）
        self.panel_open = False
        self.clip = None
        self.alert = None        # {'layer', 'dur', 'until', 'bar'}
        self.drag = None         # 正在被拖动的窗口 {'hwnd', 'placement', 'topmost'}
        self.drop_kind = None    # None / 'near' / 'hot' / 'full'
        self._drop_layers = {}
        self.shape_rect = (0, 0, 0, 0)
        self.pill_rects = []
        self._pill_cache = (None, None)
        self._hires = False
        self.surface = LayeredSurface(self.hwnd)
        self.last_frame_key = None
        self.redraw(force=True)

    # -- 坐标
    def panel_left(self):
        return (self.W - self.panel_w) / 2

    def cursor_local(self):
        x, y = tf.cursor_pos()
        return x - self.x, y - self.y

    def in_tab(self, lx, ly):
        half = self.tab_w / 2 + self.ear
        return abs(lx - self.W / 2) <= half and ly <= self.tab_h + 1

    def in_expanded(self, lx, ly):
        m = self.P(10)
        pl = self.panel_left()
        if pl - self.ear - m <= lx <= pl + self.panel_w + self.ear + m and ly <= self.panel_h + m:
            return True
        if self.card_rect:
            x0, y0, x1, y1 = self.card_rect
            return x0 - m <= lx <= x1 + m and y0 - self.P(CARD_GAP) - m <= ly <= y1 + m
        return False

    def item_at(self, lx, ly):
        if ly > self.panel_h or self.t < 0.99:
            return None
        rel = lx - self.panel_left()
        for i, cxp in enumerate(self.centers):
            if abs(rel - cxp) <= self.P(ITEM_W) / 2:
                return i
        return None

    # -- 状态
    def tab_dims(self):
        return self.tab_w, self.tab_h, self.P(TAB_R)

    def dims_of(self, desc):
        state, layer = desc
        if state == 'tab':
            return self.tab_dims()
        if state == 'panel':
            return self.panel_w, self.panel_h, self.P(PANEL_R)
        r = self.app.cpx(PANEL_R) if state == 'clip' else min(self.P(PANEL_R), layer.height // 2)
        return layer.width, layer.height, r

    def desired(self):
        """现在应该显示什么。优先级：拖窗口 > 用户正在看的面板 > 提醒 > 复制内容 > 小条。"""
        if self.drag is not None and self.drop_kind:
            return 'drop', self.drop_layer(self.drop_kind)
        if self.panel_open:
            return 'panel', None
        if self.alert is not None:
            return 'alert', self.alert['layer']
        if self.clip is not None:
            return 'clip', self.clip['layer']
        return 'tab', None

    @staticmethod
    def desc_key(desc):
        return desc[0], id(desc[1]) if desc[1] is not None else 0

    def show_clip(self, clip):
        self.clip = clip
        self.app.clip_pinned = True

    def dismiss_clip(self):
        self.clip = None
        self.app.clip_pinned = False

    def show_alert(self, layer, dur, bar=False):
        self.alert = {'layer': layer, 'dur': dur, 'until': None, 'bar': bar}

    def drop_layer(self, kind):
        key = (kind, len(self.app.hangs.items))
        if key not in self._drop_layers:
            self._drop_layers[key] = build_drop_layer(kind, key[1], self.P)
        return self._drop_layers[key]

    def raise_top(self):
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def hires(self, on):
        """动画期间把系统定时器精度调到 1 毫秒（默认 15.6 毫秒一跳，动画会一顿一顿的），动画完马上恢复。"""
        if on != self._hires:
            (winmm.timeBeginPeriod if on else winmm.timeEndPeriod)(1)
            self._hires = on

    # -- 变形动画
    def start_morph(self, desc, now):
        frm = self.current_dims(now)
        out = self.visible_desc(now)
        state = desc[0]
        dur, ease = {'panel': (EXPAND_TIME, 'out'), 'tab': (COLLAPSE_TIME, 'inout'),
                     'drop': (DROP_TIME, 'back')}.get(state, (CLIP_IN_TIME, 'back'))
        if out[0] in ('clip', 'alert') and state == 'tab':
            dur = CLIP_OUT_TIME
        if state == 'alert' and self.alert['until'] is None:
            self.alert['until'] = now + dur + self.alert['dur']
        self.anim = {'from': frm, 'to': self.dims_of(desc), 'out': out, 'in': desc, 'start': now, 'dur': dur,
                     'ease': ease}
        self.target_key = self.desc_key(desc)
        self.t = 0.0
        if state != 'tab':
            self.raise_top()
        self.hires(True)

    def anim_state(self, now):
        a = self.anim
        k = min(1.0, (now - a['start']) / a['dur'])
        if a['ease'] == 'back':  # 略微冲过头再回来，像灵动岛
            c1 = 1.25
            e = 1 + (c1 + 1) * (k - 1) ** 3 + c1 * (k - 1) ** 2
        elif a['ease'] == 'out':
            e = 1 - (1 - k) ** 3
        else:
            e = k * k * (3 - 2 * k)
        (w0, h0, r0), (w1, h1, r1) = a['from'], a['to']
        return (w0 + (w1 - w0) * e, h0 + (h1 - h0) * e, r0 + (r1 - r0) * min(1.0, e)), k

    def current_dims(self, now):
        return self.anim_state(now)[0] if self.anim else self.cur_dims

    def visible_desc(self, now):
        if self.anim:
            return self.anim['in'] if self.anim_state(now)[1] > 0.5 else self.anim['out']
        return self.state, self.content

    # -- 每帧
    def step(self):
        """返回下次调用的间隔毫秒。"""
        if self.hidden:
            return 400
        now = time.perf_counter()
        lx, ly = self.cursor_local()
        self.update_drag(lx, ly)
        self.update_hover(lx, ly, now)
        if self.alert is not None and self.alert['until'] is not None and now >= self.alert['until']:
            self.alert = None
        desc = self.desired()
        if self.desc_key(desc) != self.target_key:
            self.start_morph(desc, now)
        if self.anim is not None:
            if self.anim_state(now)[1] >= 1:
                self.state, self.content = self.anim['in']
                self.cur_dims = self.anim['to']
                self.anim = None
                self.t = 1.0 if self.state == 'panel' else 0.0
                self.hires(False)
            self.redraw(force=True)
            return 1  # 定时器已调到 1 毫秒精度，尽快画下一帧；每帧按真实时间推进
        if self.state == 'alert' and self.alert and self.alert['bar']:
            self.redraw(force=True)  # 底部倒计时条
            return 16
        if self.state == 'tab':
            self.panel_layer(datetime.now(timezone.utc))  # 空闲时预先画好面板，展开第一帧不卡
        self.redraw()
        return 30 if self.state == 'panel' else 16

    def update_hover(self, lx, ly, now):
        if self.panel_open:
            if self.in_expanded(lx, ly):
                self.leave_since = None
                it = self.item_at(lx, ly)
                if it is not None:
                    self.hover = it
            else:
                self.leave_since = self.leave_since or now
                if now - self.leave_since >= LEAVE_DELAY:
                    self.panel_open, self.hover, self.leave_since = False, None, None
            return
        # 只有显示普通小条时才能悬停展开（挂着复制内容、提醒、正在拖窗口时不展开）
        idle_tab = self.drag is None and self.alert is None and self.clip is None
        if idle_tab and self.in_tab(lx, ly) and not tf.key_down(tf.VK_LBUTTON):
            self.enter_since = self.enter_since or now
            if now - self.enter_since >= ENTER_DELAY:
                self.panel_open, self.enter_since = True, None
        else:
            self.enter_since = None

    def update_drag(self, lx, ly):
        if self.drag is None:
            self.drop_kind = None
            return
        dx = abs(lx - self.W / 2)
        full = self.app.hangs.full()
        if dx <= self.P(160) and ly <= self.P(90):
            self.drop_kind = 'full' if full else 'hot'
        elif dx <= self.P(420) and ly <= self.P(240):
            self.drop_kind = 'full' if full else 'near'
        else:
            self.drop_kind = None

    # -- 绘制
    def redraw(self, force=False):
        now = datetime.now(timezone.utc)
        on_time_card = self.state == 'panel' and self.hover is not None and self.items[self.hover][0] == 'time'
        key = (self.state, id(self.content), self.hover,
               now.strftime('%H%M%S') if on_time_card else now.strftime('%H%M'),
               self.app.data_version, self.app.cfg['zone'], self.app.hangs.version, self.anim is None)
        if not force and key == self.last_frame_key:
            return
        self.last_frame_key = key
        img, ox = self.compose(now)
        self.surface.update(img, self.x + ox, self.y)

    def _layer_for(self, desc, now):
        state, lay = desc
        if state == 'tab':
            lay = self.tab_layer(now)
            return lay, round(self.W / 2 - lay.width / 2)
        if state == 'panel':
            return self.panel_layer(now), round(self.panel_left())
        return lay, round(self.W / 2 - lay.width / 2)

    def on_black(self, lay):
        """把一层内容铺到黑底上（灵动岛本来就是黑的），结果按图层缓存。"""
        key = id(lay)
        hit = self._black_cache.get(key)
        if hit is not None and hit[0] is lay:
            return hit[1]
        rgb = Image.new('RGB', lay.size, (0, 0, 0))
        rgb.paste(lay, (0, 0), lay)
        if len(self._black_cache) > 16:
            self._black_cache.clear()
        self._black_cache[key] = (lay, rgb)
        return rgb

    def prepare(self, layer, radius):
        """内容到达时（在后台线程里）先把黑底图、阴影、停稳后的整帧都算好，弹出动画最后一帧不卡。"""
        self.on_black(layer)
        self.settled_frame(layer, radius)

    def settled_frame(self, layer, radius):
        """复制内容停稳后的整帧（带阴影）：内容不变，算一次缓存起来。返回 ((BGRA, 宽, 高), x 偏移, 外形矩形)。"""
        key = id(layer)
        hit = self._settled_cache.get(key)
        if hit is not None and hit[0] is layer:
            return hit[1]
        P = self.P
        mask = fast_notch_mask(layer.width, layer.height, radius, self.ear)
        sw, sh_ = mask.size
        x0 = round(self.W / 2 - sw / 2)
        shadow, pad = self.shadow_for(mask, True)
        base = Image.new('RGB', (sw, sh_), (0, 0, 0))
        base.paste(self.on_black(layer), (round(self.W / 2 - layer.width / 2) - x0, 0))
        body = base.convert('RGBA')
        body.putalpha(mask)
        img = Image.new('RGBA', (sw + 2 * pad, sh_ + pad + P(4)), (0, 0, 0, 0))
        img.alpha_composite(shadow, (0, -pad + P(4)))
        img.alpha_composite(body, (pad, 0))
        out = ((img.tobytes('raw', 'BGRa'), img.width, img.height), x0 - pad, (x0, 0, x0 + sw, sh_))
        if len(self._settled_cache) > 6:
            self._settled_cache.clear()
        self._settled_cache[key] = (layer, out)
        return out

    def shadow_for(self, mask, cache):
        key = mask.size
        if cache and key in self._shadow_cache:
            return self._shadow_cache[key]
        sh = fast_shadow(mask, self.P(9), 0.55)
        if cache:
            if len(self._shadow_cache) > 24:
                self._shadow_cache.clear()
            self._shadow_cache[key] = sh
        return sh

    def compose(self, now):
        """画一帧。只画出有东西的那块区域，返回 (图, 这块在窗口里的 x 偏移)。"""
        P = self.P
        nowp = time.perf_counter()
        if self.anim is not None:
            (w, h, r), k = self.anim_state(nowp)
            layers = [(self.anim['out'], max(0.0, 1 - k / 0.45)), (self.anim['in'], max(0.0, (k - 0.4) / 0.6))]
            moving = True
        else:
            w, h, r = self.cur_dims
            layers = [((self.state, self.content), 1.0)]
            moving = False
            if self.state == 'clip':  # 停稳的复制内容是静止画面，用预先算好的整帧
                frame, ox, self.shape_rect = self.settled_frame(self.content, r)
                self.card_rect, self.pill_rects = None, []
                return frame, ox
        mask = fast_notch_mask(w, h, r, self.ear)
        sw, sh_ = mask.size
        x0 = round(self.W / 2 - sw / 2)
        # 阴影只在停稳后画（每帧算阴影最贵，动画中看不出有没有）
        shadow = self.shadow_for(mask, True) if not moving and sh_ > self.tab_h * 1.3 else None

        # 要画的其它东西：详情卡片、右侧悬挂按钮
        card = None
        if not moving and self.state == 'panel' and self.hover is not None:
            card = self.card_layer(self.hover, now)
        tab_alpha = sum(a for (st, _), a in layers if st == 'tab')
        pill = self.pill_layer() if self.app.hangs.items and tab_alpha > 0.02 else None

        boxes = [(x0, sw + x0, sh_)]
        if shadow:
            boxes.append((x0 - shadow[1], x0 + sw + shadow[1], sh_ + shadow[1] + P(4)))
        if card is not None:
            tip_h = P(7)
            cw, ch = card.size
            item_x = self.panel_left() + self.centers[self.hover]
            cxl = int(min(max(item_x - cw / 2, P(SHADOW)), self.W - cw - P(SHADOW)))
            cyt = int(self.panel_h + P(CARD_GAP) - tip_h)
            chrome, cpad = self.card_chrome(cw, ch, int(item_x - cxl), tip_h)
            boxes.append((cxl - cpad, cxl - cpad + chrome.width, cyt - cpad + chrome.height))
        pill_x = int(self.W / 2 + self.tab_w / 2 + self.ear + P(6))
        if pill is not None:
            boxes.append((pill_x, pill_x + pill[0].width, pill[0].height))
        ox = max(0, min(b[0] for b in boxes))
        cw_ = min(self.W, max(b[1] for b in boxes)) - ox
        ch_ = min(self.H, max(b[2] for b in boxes))

        # 内容：每层都是“铺在黑底上”的 RGB 图（缓存），淡入淡出 = 亮度乘个系数。
        # 大的那层直接贴，小的那层只在自己那块区域里和底下取亮（整图取亮要 3 毫秒多）
        parts = []
        for desc, alpha in layers:
            if alpha <= 0.01:
                continue
            lay, lx = self._layer_for(desc, now)
            rgb = self.on_black(lay)
            lx -= x0
            part = rgb.crop((max(0, -lx), 0, min(rgb.width, sw - lx), min(rgb.height, sh_)))
            if part.width <= 0 or part.height <= 0:
                continue
            if alpha < 0.99:
                part = part.point([int(v * alpha) for v in range(256)] * 3)
            parts.append((part, max(0, lx)))
        parts.sort(key=lambda pp: -pp[0].width * pp[0].height)
        base = Image.new('RGB', (sw, sh_), (0, 0, 0))
        for i, (part, px) in enumerate(parts):
            region = (px, 0, px + part.width, part.height)
            if i:
                part = ImageChops.lighter(base.crop(region), part)
            base.paste(part, region)
        alert_bar = self.alert is not None and self.alert['bar'] and self.alert['until'] and \
            self.visible_desc(nowp)[0] == 'alert'
        if alert_bar:  # 休息提醒底部的倒计时条
            frac = max(0.0, min(1.0, (self.alert['until'] - nowp) / self.alert['dur']))
            bx0, bx1, by = self.ear + P(28), sw - self.ear - P(28), sh_ - P(12)
            if frac > 0 and by > 0:
                ImageDraw.Draw(base).rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * frac, by + P(3)],
                                                       radius=P(1.5), fill=WARM[:3])
        self.shape_rect = (x0, 0, x0 + sw, sh_)
        if len(boxes) == 1:
            # 最常见的情况（动画中、复制内容、提醒）：画布就是黑色外形本身。
            # 外形边缘全是黑色，预乘 alpha 前后一样，直接把 B、G、R、外形遮罩拼成 BGRA 交给系统
            r_, g_, b_ = base.split()
            data = Image.merge('RGBA', (b_, g_, r_, mask)).tobytes()
            self.card_rect, self.pill_rects = None, []
            return (data, sw, sh_), x0
        # 有阴影 / 卡片 / 右侧按钮：在透明画布上正常合成
        body = base.convert('RGBA')
        body.putalpha(mask)
        img = Image.new('RGBA', (max(1, cw_), max(1, ch_)), (0, 0, 0, 0))
        if shadow:
            img.alpha_composite(shadow[0], (x0 - shadow[1] - ox, -shadow[1] + P(4)))
        img.alpha_composite(body, (x0 - ox, 0))
        self.card_rect = None
        if card is not None:
            img.alpha_composite(chrome, (cxl - cpad - ox, cyt - cpad))
            img.alpha_composite(card, (cxl - ox, cyt + tip_h))
            self.card_rect = (cxl, cyt, cxl + cw, cyt + tip_h + ch)

        self.pill_rects = []
        if pill is not None:
            pimg, rects = pill
            if tab_alpha < 0.99:
                pimg = pimg.copy()
                pimg.putalpha(pimg.getchannel('A').point(lambda v, a=tab_alpha: int(v * a)))
            img.alpha_composite(pimg, (pill_x - ox, 0))
            self.pill_rects = [((pill_x + a, b, pill_x + c, d), act) for (a, b, c, d), act in rects]
        return img, ox

    def pill_layer(self):
        """灵动岛右边的小块：每个悬挂窗口一个图标（点了单独还原），最后一个关闭按钮（全部结束悬挂）。"""
        key = self.app.hangs.version
        if self._pill_cache[0] == key:
            return self._pill_cache[1]
        P = self.P
        icon, gap, btn = P(18), P(7), P(20)
        items = self.app.hangs.items
        w = P(12) + len(items) * (icon + gap) + btn + P(10)
        h = self.tab_h
        mask = fast_notch_mask(w, h, P(TAB_R), self.ear)
        img = Image.new('RGBA', mask.size, BLACK)
        d = ImageDraw.Draw(img)
        rects = []
        x, cy = self.ear + P(12), h / 2
        for it in items:
            if it.icon is not None:
                img.alpha_composite(it.icon.resize((icon, icon), Image.LANCZOS), (int(x), int(cy - icon / 2)))
            else:
                d.ellipse([x, cy - icon / 2, x + icon, cy + icon / 2], fill=(58, 58, 62, 255))
                d.text((x + icon / 2, cy), (it.title or '?')[:1], font=font('cnb', P(10)), fill=WHITE, anchor='mm')
            rects.append(((int(x - gap / 2), 0, int(x + icon + gap / 2), h), ('release', it.hwnd)))
            x += icon + gap
        bx = x + btn / 2
        d.ellipse([bx - btn / 2, cy - btn / 2, bx + btn / 2, cy + btn / 2], fill=(64, 64, 68, 255))
        k, lw = btn * 0.2, max(2, P(1.8))
        d.line([bx - k, cy - k, bx + k, cy + k], fill=WHITE, width=lw)
        d.line([bx - k, cy + k, bx + k, cy - k], fill=WHITE, width=lw)
        rects.append(((int(x - gap / 2), 0, int(x + btn + P(10)), h), ('release_all', None)))
        img.putalpha(mask)
        self._pill_cache = (key, (img, rects))
        return self._pill_cache[1]

    def card_chrome(self, cw, ch, tip_x, tip_h):
        """卡片的阴影 + 边框 + 底色，按尺寸和小尖角位置缓存。"""
        key = (cw, ch, tip_x)
        if key not in self._chrome_cache:
            if len(self._chrome_cache) > 30:
                self._chrome_cache.clear()
            P = self.P
            cm = card_mask(cw, ch, P(16), tip_x, tip_h)
            csh, cpad = shadow_of(cm, P(10), 0.6)
            out = Image.new('RGBA', csh.size, (0, 0, 0, 0))
            out.alpha_composite(csh, (0, P(4)))
            edge = Image.new('RGBA', cm.size, CARD_EDGE)
            edge.putalpha(cm)
            out.alpha_composite(edge, (cpad, cpad))
            inner = cm.resize((cm.width - 2, cm.height - 2), Image.LANCZOS)
            bgl = Image.new('RGBA', inner.size, CARD_BG)
            bgl.putalpha(inner)
            out.alpha_composite(bgl, (cpad + 1, cpad + 1))
            self._chrome_cache[key] = (out, cpad)
        return self._chrome_cache[key]

    def tab_layer(self, now):
        stamp = (now.strftime('%H%M'), self.app.data_version)
        if self._tab_cache[0] == stamp:
            return self._tab_cache[1]
        self._tab_cache = (stamp, self._draw_tab(now))
        return self._tab_cache[1]

    def _draw_tab(self, now):
        P = self.P
        img = Image.new('RGBA', (self.tab_w, self.tab_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f = font('cn', P(11.5))
        cn = now.astimezone(tp.get_tz('cn'))
        la = now.astimezone(tp.get_tz('la'))
        y = self.tab_h / 2
        d.text((P(16), y), f'京 {cn:%H:%M}', font=f, fill=WHITE, anchor='lm')
        d.text((P(16) + d.textlength(f'京 {cn:%H:%M}', font=f) + P(10), y), f'洛 {la:%H:%M}', font=f, fill=GRAY,
               anchor='lm')
        dots = [self.app.view(k, i) for k, i in self.items if k in ('claude', 'codex')]
        x = self.tab_w - P(16)
        for v in reversed(dots):
            c = remain_color(v['main']) if v['main'] is not None else DIM
            rr = P(3.2)
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=c)
            x -= P(10)
        return img

    def panel_layer(self, now):
        stamp = (now.strftime('%H%M'), self.app.data_version)
        if self._panel_cache[0] == stamp:
            return self._panel_cache[1].copy()
        P = self.P
        img = Image.new('RGBA', (int(self.panel_w), self.panel_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cy = P(42)
        fv, fl = font('num', P(15)), font('cn', P(10.5))
        for dx in self.dividers:
            d.line([dx, P(24), dx, self.panel_h - P(22)], fill=DIVIDER, width=max(1, P(1)))
        for (kind, k), cxp in zip(self.items, self.centers):
            if kind == 'time':
                local = now.astimezone(tp.get_tz(k))
                day = 6 <= local.hour < 18
                col = DAY if day else NIGHT
                frac = (local.hour * 60 + local.minute) / 1440
                ring(img, cxp, cy, P(23), P(3.6), frac, col)
                clock_icon(img, cxp, cy, P(11), local, WHITE)
                value = f'{local:%H:%M}'
                label = f'{tp.ZONES[k][0]} 周{tp.WEEK_CN[local.weekday()]}'
            else:
                v = self.app.view(kind, k)
                outer, inner = v['five'], v['week']
                if outer is None:
                    outer, inner = inner, None
                if outer is not None:
                    ring(img, cxp, cy, P(23), P(3.8), outer / 100, remain_color(outer))
                else:
                    ring(img, cxp, cy, P(23), P(3.8), 0, DIM)
                if inner is not None:
                    ring(img, cxp, cy, P(16.5), P(2.6), inner / 100, dim(remain_color(inner), 0.75))
                if kind == 'codex':
                    d.text((cxp, cy + P(0.5)), '>_', font=font('mono', P(13)), fill=WHITE, anchor='mm')
                elif kind == 'ctotal':
                    d.text((cxp, cy), '∑', font=font('sym', P(15)), fill=CLAUDE, anchor='mm')
                else:
                    claude_star(img, cxp, cy, P(8.5))
                value = f'{v["main"]:.0f}%' if v['main'] is not None else '—'
                label = v['short'] if v['state'] in ('ok', 'loading') else v['status']
            d.text((cxp, P(84)), value, font=fv, fill=WHITE, anchor='mm')
            d.text((cxp, P(103)), label, font=fl, fill=GRAY, anchor='mm')
        self._panel_cache = (stamp, img)
        return img.copy()

    # -- 详情卡片
    def card_layer(self, idx, now):
        kind, k = self.items[idx]
        key = (idx, self.app.data_version, self.app.cfg['zone'],
               now.strftime('%H%M%S') if kind == 'time' else now.strftime('%H%M'))
        if key not in self._card_cache:
            if len(self._card_cache) > 40:
                self._card_cache.clear()
            spec = getattr(self, f'spec_{kind}')(k, now)
            card = self.render_card(spec)
            avail = (self.mon[3] - self.mon[1]) - self.panel_h - self.P(CARD_GAP) - self.P(SHADOW) - self.P(10)
            if card.height > avail:
                f = avail / card.height * self.app.scale
                card = self.render_card(spec, lambda v: int(round(v * f)))
            self._card_cache[key] = card
        return self._card_cache[key]

    def spec_time(self, k, now):
        local = now.astimezone(tp.get_tz(k))
        day = 6 <= local.hour < 18
        other = 'la' if k == 'cn' else 'cn'
        o = now.astimezone(tp.get_tz(other))
        diff = (local.utcoffset() - o.utcoffset()).total_seconds() / 3600
        rel = f'比{tp.ZONES[other][0]}{"快" if diff > 0 else "慢"} {abs(diff):g} 小时'
        rows = []
        for z in tp.ORDER:
            if z == k:
                continue
            t = now.astimezone(tp.get_tz(z))
            dd = (t.date() - local.date()).days
            rows.append((tp.ZONES[z][0], f'{t:%H:%M}  {tp.fmt_date(t)}' + {1: ' 次日', -1: ' 前一天'}.get(dd, ''),
                         None, None))
        return {'icon': ('clock', local, DAY if day else NIGHT), 'title': f'{tp.ZONES[k][0]}时间',
                'right': tp.abbr(local, k), 'big': f'{local:%H:%M:%S}', 'big_color': DAY if day else NIGHT,
                'caption': '白天' if day else '夜间', 'sub': f'{tp.fmt_date(local)} · {rel}',
                'bar': (local.hour * 60 + local.minute) / 1440, 'bar_color': DAY if day else NIGHT, 'rows': rows}

    def _reset(self, key, at, now):
        return tf.UsagePanel.reset_text(key, at, now, tp.get_tz(self.app.cfg['zone']))

    def _token_rows(self, periods_list, models=None, inferred=0.0):
        """本机日志里的 token 用量几行。periods_list: 一个或多个账号的 periods（合计时相加）。"""
        if not self.app.tokens_ready:
            return [('本机 Token 用量', '统计中…', None, None)]
        if not periods_list:
            return [('本机 Token 用量', '本机没有这个账号的记录', None, None)]
        rows = [('本机 Token 用量', None, None, None)]
        for key, name in (('today', '今天'), ('week', '近 7 天'), ('month', '本月'), ('all', '累计')):
            tot = sum(p[key][0] for p in periods_list)
            out = sum(p[key][1] for p in periods_list)
            rows.append((name, f'{ts.fmt_tokens(tot)}  ·  输出 {ts.fmt_tokens(out)}' if tot else '0', None, None))
        models = [(m, n) for m, n in (models or []) if n and not m.startswith('<')]
        if models:
            total = sum(n for _, n in models)
            rows.append(('常用模型', f'{models[0][0]}  {models[0][1] / total:.0%}', None, None))
        if inferred > 0.05:
            rows.append(('说明', f'{inferred:.0%} 是早期会话，按当时登录的账号推断', None, None))
        return rows

    def _account_token_rows(self, kind, k):
        st = self.app.tokens.get((kind, k))
        if not st:
            return self._token_rows([])
        return self._token_rows([st['periods']], st['models'], st['inferred'])

    def _account_spec(self, kind, k, now):
        v = self.app.view(kind, k)
        icon = ('claude',) if kind == 'claude' else ('codex',)
        title = f'{"Claude" if kind == "claude" else "Codex"} · {v["short"]}'
        if v['state'] in ('nologin', 'expired') or (v['state'] == 'error' and not v['data']):
            msg = {'nologin': ('未登录', '点击圆环打开登录窗口，在浏览器里授权'),
                   'expired': ('登录失效', '点击圆环重新登录'),
                   'error': ('获取失败', '网络不通？稍后会自动重试')}[v['state'] if v['state'] != 'error' else 'error']
            return {'icon': icon, 'title': title, 'right': v['plan'], 'big': msg[0], 'big_color': AMBER,
                    'caption': '', 'sub': msg[1], 'bar': None, 'rows': self._account_token_rows(kind, k)}
        if v['state'] == 'loading':
            return {'icon': icon, 'title': title, 'right': '', 'big': '读取中…', 'big_color': GRAY, 'caption': '',
                    'sub': '', 'bar': None, 'rows': self._account_token_rows(kind, k)}
        d = v['data']
        rows = []
        if v['five'] is not None:
            big, cap = v['five'], '5 小时剩余'
            sub = self._reset('five_hour', d['five_hour']['resets_at'], now)
            if v['week'] is not None:
                rows.append(('本周剩余', f'{v["week"]:.0f}% · {self._reset("seven_day", d["seven_day"]["resets_at"], now)}',
                             v['week'] / 100, remain_color(v['week'])))
        elif v['week'] is not None:
            big, cap = v['week'], '本周剩余'
            sub = self._reset('seven_day', d['seven_day']['resets_at'], now)
            rows.append(('5 小时', '这个套餐没有 5 小时限制', None, None))
        else:
            big, cap, sub = None, '', '没有用量数据'
        if d.get('limit_reached'):
            rows.append(('状态', '已触顶，等重置', None, None))
        rows += self._account_token_rows(kind, k)
        rows.append(('账号', d.get('email') or v['email'] or '', None, None))
        upd = datetime.fromtimestamp(d['time']).strftime('%H:%M')
        rows.append(('更新于', upd + ('（离线，显示的是旧数据）' if v['state'] == 'error' else ''), None, None))
        return {'icon': icon, 'title': title, 'right': v['plan'],
                'big': f'{big:.0f}%' if big is not None else '—', 'big_color': remain_color(big) if big is not None else GRAY,
                'caption': cap, 'sub': sub, 'bar': big / 100 if big is not None else None,
                'bar_color': remain_color(big) if big is not None else GRAY, 'rows': rows}

    def spec_claude(self, k, now):
        return self._account_spec('claude', k, now)

    def spec_codex(self, k, now):
        return self._account_spec('codex', k, now)

    def spec_ctotal(self, _, now):
        views = [self.app.view('claude', i) for i in range(len(self.app.cfg['accounts']))]
        t = self.app.view('ctotal', None)
        rows = []
        for v in views:
            if v['five'] is not None:
                txt = f'5h {v["five"]:.0f}%' + (f' · 周 {v["week"]:.0f}%' if v['week'] is not None else '')
                rows.append((v['short'], txt, v['five'] / 100, remain_color(v['five'])))
            else:
                rows.append((v['short'], v['status'] or '—', None, None))
        if t['week'] is not None:
            rows.append(('本周合计剩余', f'{t["week"]:.0f}%', t['week'] / 100, remain_color(t['week'])))
        sts = [self.app.tokens[('claude', i)] for i in range(len(views)) if ('claude', i) in self.app.tokens]
        merged = {}
        for st in sts:
            for m, n in st['models']:
                merged[m] = merged.get(m, 0) + n
        rows += self._token_rows([st['periods'] for st in sts], sorted(merged.items(), key=lambda x: -x[1]))
        n = sum(1 for v in views if v['five'] is not None)
        big = t['five']
        return {'icon': ('total',), 'title': f'Claude 合计 · {len(views)} 个账号', 'right': f'{n} 个在线',
                'big': f'{big:.0f}%' if big is not None else '—',
                'big_color': remain_color(big) if big is not None else GRAY, 'caption': '5 小时合计剩余',
                'sub': f'相当于 {sum(v["five"] for v in views if v["five"] is not None) / 100:.1f} 个完整的 5 小时额度'
                if big is not None else '还没有数据',
                'bar': big / 100 if big is not None else None,
                'bar_color': remain_color(big) if big is not None else GRAY, 'rows': rows}

    def render_card(self, s, P=None):
        P = P or self.P
        w, pad, row_h = P(CARD_W), P(16), P(30)
        has_bar = s.get('bar') is not None
        head_h = P(26)
        h = pad + P(26) + P(44) + (P(22) if s['sub'] else 0) + (P(16) if has_bar else 0) + P(10) + \
            sum(head_h if r[1] is None else row_h for r in s['rows'])
        img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        y = pad
        ic = s['icon']
        icx, icy = pad + P(9), y + P(10)
        if ic[0] == 'clock':
            clock_icon(img, icx, icy, P(8), ic[1], ic[2])
        elif ic[0] == 'claude':
            claude_star(img, icx, icy, P(8))
        elif ic[0] == 'total':
            d.text((icx, icy), '∑', font=font('sym', P(15)), fill=CLAUDE, anchor='mm')
        else:
            d.text((icx, icy), '>_', font=font('mono', P(12)), fill=WHITE, anchor='mm')
        d.text((pad + P(24), icy), s['title'], font=font('cnb', P(13)), fill=WHITE, anchor='lm')
        if s['right']:
            d.text((w - pad, icy), s['right'], font=font('cn', P(11)), fill=GRAY, anchor='rm')
        y += P(26)
        fb = font('num', P(32)) if s['big'][:1].isdigit() or s['big'] == '—' else font('cnb', P(24))
        d.text((pad, y + P(36)), s['big'], font=fb, fill=s['big_color'], anchor='ls')
        if s['caption']:
            d.text((pad + d.textlength(s['big'], font=fb) + P(10), y + P(34)), s['caption'], font=font('cn', P(12)),
                   fill=GRAY, anchor='ls')
        y += P(44)
        if s['sub']:
            d.text((pad, y), s['sub'], font=font('cn', P(11.5)), fill=GRAY, anchor='la')
            y += P(22)
        if has_bar:
            bar(d, pad, y, w - pad, P(6), s['bar'], s['bar_color'])
            y += P(16)
        for label, value, frac, color in s['rows']:
            if value is None:  # 小标题
                d.text((pad, y + P(16)), label, font=font('cn', P(10.5)), fill=DIM, anchor='lm')
                y += head_h
                continue
            d.line([pad, y, w - pad, y], fill=DIVIDER, width=1)
            d.text((pad, y + P(13)), label, font=font('cn', P(11.5)), fill=WHITE, anchor='lm')
            d.text((w - pad, y + P(13)), value, font=font('cn', P(11)), fill=GRAY, anchor='rm')
            if frac is not None:
                bar(d, pad, y + P(23), w - pad, P(3), frac, color)
            y += row_h
        return img

    # -- 交互
    def on_click(self, e):
        ex, ey = e.x_root - self.x, e.y_root - self.y
        for (x0, y0, x1, y1), (act, hwnd) in self.pill_rects:
            if x0 <= ex <= x1 and y0 <= ey <= y1:
                if act == 'release':
                    self.app.hangs.release(hwnd)
                else:
                    self.app.hangs.release_all()
                return
        x0, y0, x1, y1 = self.shape_rect
        inside = x0 <= ex <= x1 and y0 <= ey <= y1
        shown = self.visible_desc(time.perf_counter())[0]
        if shown == 'clip':
            if inside:
                self.dismiss_clip()
            return
        if shown == 'alert':
            if inside:
                self.alert = None
            return
        if shown != 'panel' or self.anim is not None:
            return
        idx = self.item_at(ex, ey)
        if idx is None and self.card_rect and self.hover is not None:
            x0, y0, x1, y1 = self.card_rect
            if x0 <= ex <= x1 and y0 <= ey <= y1:
                idx = self.hover
        if idx is not None:
            kind, k = self.items[idx]
            if kind in ('claude', 'codex') and self.app.view(kind, k)['state'] in ('nologin', 'expired'):
                self.app.login(kind, k)
                return
        self.app.refresh()

    def set_hidden(self, hidden):
        if hidden != self.hidden:
            self.hidden = hidden
            user32.ShowWindow(self.hwnd, 0 if hidden else 4)  # SW_HIDE / SW_SHOWNOACTIVATE


# ---------------------------------------------------------------- 程序
def autostart_cmd():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith('python.exe'):
        exe = exe[:-len('python.exe')] + 'pythonw.exe'
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, tf.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, tf.RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, tf.RUN_NAME)
            except OSError:
                pass


class NotchApp:
    def __init__(self):
        self.cfg = tf.load_config()
        self.cfg.setdefault('codex_accounts', [
            {'path': '~/.codex/auth.json', 'email': None},
            {'path': '~/.codex-usage-2/auth.json', 'email': None, 'login_home': '~/.codex-usage-2'}])
        self.events = queue.Queue()
        self.state = {}   # (kind, i) -> (状态, 数据)
        self.data = {}    # (kind, i) -> 最近一次成功的数据
        self.data_version = 0
        self.tokens = {}          # (kind, i) -> token 统计
        self.tokens_ready = False
        self.clip_pinned = False   # 有复制内容挂在灵动岛上（粘贴检测只在这时工作）
        # Codex 账号 id 用来认领本机日志；没登录的从账号切换器的记录里按邮箱找
        for acc in self.cfg['codex_accounts']:
            if not acc.get('account_id'):
                try:
                    acc['account_id'] = cx.identity(acc['path'])['account_id']
                except Exception:
                    acc['account_id'] = cx.account_id_for_email(acc.get('email'))
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels('1i') / 96
        self.cfg.pop('notch_scale', None)
        self.cfg.setdefault('clip_scale', 2.0)
        self.build_menu()
        self.hangs = wh.HangManager((0, 0, 0, 0), 0, 0, 16)
        self.notch = Notch(self)
        n = self.notch
        self.hangs = wh.HangManager(n.mon, n.mon[1] + n.tab_h + self.px(10), self.px(12), self.px(18) * 2)
        self.hangs.recover()  # 上次没正常结束的悬挂，先还原
        wh.DragWatcher(lambda kind, hwnd: self.events.put((kind, hwnd))).start()
        self.rest_active, self.rest_tick = 0.0, time.monotonic()
        self.root.after(5000, self.rest_check)
        self.popup = tf.Popup(self)
        tf.SelectionWatcher(self).start()
        self.worker = Worker(self)
        self.worker.start()
        self.token_worker = TokenWorker(self)
        self.token_worker.start()
        cw.ClipWatcher(self, self.on_clip).start()
        cw.PasteWatcher(self, lambda: self.events.put(('paste',))).start()
        self.loop()
        self.poll()
        self.check_fullscreen()

    def px(self, v):
        return int(round(v * self.scale))

    def cpx(self, v):
        """复制 / 截图提示用的尺寸：再乘上“复制提示大小”倍数。"""
        return int(round(v * self.scale * float(self.cfg.get('clip_scale', 2.0))))

    def on_clip(self, clip):
        """剪贴板线程里调用：先把预览画好，再交给界面线程弹出。"""
        try:
            clip['layer'] = build_clip_layer(clip, self.cpx)
            self.notch.prepare(clip['layer'], self.cpx(PANEL_R))
        except Exception:
            return
        self.events.put(('clip', clip))

    # -- 数据视图
    def view(self, kind, i):
        if kind == 'ctotal':
            vs = [self.view('claude', j) for j in range(len(self.cfg['accounts']))]
            fives = [v['five'] for v in vs if v['five'] is not None]
            weeks = [v['week'] for v in vs if v['week'] is not None]
            five = sum(fives) / len(fives) if fives else None
            week = sum(weeks) / len(weeks) if weeks else None
            return {'five': five, 'week': week, 'main': five if five is not None else week, 'state': 'ok',
                    'short': 'Claude 合计', 'status': ''}
        accs = self.cfg['accounts'] if kind == 'claude' else self.cfg['codex_accounts']
        acc = accs[i]
        st = self.state.get((kind, i), ('loading', None))[0]
        d = self.data.get((kind, i))
        email = (d and d.get('email')) or acc.get('email') or ''
        short = email.split('@')[0] or f'{"Claude" if kind == "claude" else "Codex"} {i + 1}'
        if len(short) > 10:
            short = short[:9] + '…'
        show = d if st in ('ok', 'error') else None
        five = week = None
        if show:
            if show.get('five_hour'):
                five = max(0.0, 100 - show['five_hour']['used'])
            if show.get('seven_day'):
                week = max(0.0, 100 - show['seven_day']['used'])
        plan = (d and d.get('plan') or '').replace('prolite', 'Pro Lite').replace('_', ' ')
        plan = plan[:1].upper() + plan[1:] if plan else ''
        return {'five': five, 'week': week, 'main': five if five is not None else week, 'state': st, 'data': show,
                'short': short, 'email': email, 'plan': plan,
                'status': {'nologin': '未登录', 'expired': '登录失效', 'error': '离线', 'loading': '读取中'}.get(st, '')}

    # -- 菜单
    def build_menu(self):
        self.select_var = tk.BooleanVar(value=self.cfg['select_convert'])
        self.zone_var = tk.StringVar(value=self.cfg['zone'])
        self.auto_var = tk.BooleanVar(value=False)
        m = tk.Menu(self.root, tearoff=0, font=(tf.UI_FONT, 9))
        m.add_command(label='立即刷新额度', command=self.refresh)
        m.add_separator()
        m.add_checkbutton(label='划词换算', variable=self.select_var, command=self.toggle_select)
        self.clip_var = tk.BooleanVar(value=self.cfg.get('clip_popup', True))
        m.add_checkbutton(label='复制 / 截图后在灵动岛显示', variable=self.clip_var, command=self.toggle_clip)
        zm = tk.Menu(m, tearoff=0, font=(tf.UI_FONT, 9))
        for k in tp.ORDER:
            zm.add_radiobutton(label=f'{tp.ZONES[k][0]}', value=k, variable=self.zone_var,
                               command=lambda: self.set_zone(self.zone_var.get()))
        m.add_cascade(label='没写时区时按（也用于显示重置时间）', menu=zm)
        lm = tk.Menu(m, tearoff=0, font=(tf.UI_FONT, 9))
        for i, acc in enumerate(self.cfg['accounts']):
            lm.add_command(label=f'Claude 账号{i + 1}（{acc["dir"]}）', command=lambda i=i: self.login('claude', i))
        for i, acc in enumerate(self.cfg['codex_accounts']):
            lm.add_command(label=f'Codex 账号{i + 1}（{acc.get("login_home") or os.path.dirname(acc["path"])}）',
                           command=lambda i=i: self.login('codex', i))
        m.add_cascade(label='登录账号', menu=lm)
        self.size_var = tk.DoubleVar(value=float(self.cfg.get('clip_scale', 2.0)))
        sm = tk.Menu(m, tearoff=0, font=(tf.UI_FONT, 9))
        for f in (1.0, 1.5, 2.0, 2.5):
            sm.add_radiobutton(label=f'{f:g} 倍', value=f, variable=self.size_var,
                               command=lambda: self.set_clip_scale(self.size_var.get()))
        m.add_cascade(label='复制提示大小', menu=sm)
        self.rest_var = tk.BooleanVar(value=self.cfg.get('rest_reminder', True))
        m.add_checkbutton(label='休息提醒（连续用 90 分钟）', variable=self.rest_var, command=self.toggle_rest)
        m.add_command(label='预览休息提醒', command=lambda: self.show_rest())
        m.add_command(label='结束所有悬挂', command=lambda: self.hangs.release_all())
        m.add_checkbutton(label='开机自启', variable=self.auto_var, command=lambda: set_autostart(self.auto_var.get()))
        m.add_separator()
        m.add_command(label='退出', command=self.quit)
        self.menu = m

    def on_menu(self, e):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, tf.RUN_KEY) as k:
                self.auto_var.set(winreg.QueryValueEx(k, tf.RUN_NAME)[0] == autostart_cmd())
        except OSError:
            self.auto_var.set(False)
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_rest(self):
        self.cfg['rest_reminder'] = self.rest_var.get()
        self.rest_active = 0.0
        self.save()

    def toggle_clip(self):
        self.cfg['clip_popup'] = self.clip_var.get()
        self.save()
        if not self.cfg['clip_popup']:
            self.notch.dismiss_clip()

    def toggle_select(self):
        self.cfg['select_convert'] = self.select_var.get()
        self.save()

    def set_zone(self, k):
        self.cfg['zone'] = k
        self.save()
        self.data_version += 1

    def login(self, kind, i):
        try:
            if kind == 'claude':
                acc = self.cfg['accounts'][i]
                cu.open_login(acc['dir'], acc.get('login_email') or acc.get('email'))
            else:
                acc = self.cfg['codex_accounts'][i]
                cx.open_login(acc.get('login_home') or os.path.dirname(acc['path']))
        except Exception:
            pass

    def refresh(self):
        self.worker.wake.set()

    # -- 悬挂
    def do_hang(self, d):
        ok, msg = self.hangs.hang(d['hwnd'], d['placement'], d['topmost'])
        if ok:
            self.notch.raise_top()
        else:
            self.toast(msg)

    def toast(self, text):
        self.notch.show_alert(build_toast_layer(text, self.px), 1.6)

    # -- 休息提醒
    def rest_check(self):
        now = time.monotonic()
        dt, self.rest_tick = now - self.rest_tick, now
        if dt > 60 or idle_seconds() >= REST_IDLE_RESET:
            self.rest_active = 0.0  # 睡眠过 / 离开电脑一阵子，算已经休息过
        else:
            self.rest_active += dt
        if self.cfg.get('rest_reminder', True) and self.rest_active >= self.cfg.get('rest_minutes', 90) * 60:
            self.rest_active = 0.0
            self.show_rest()
        self.hangs.prune()
        self.root.after(5000, self.rest_check)

    def show_rest(self):
        n = self.notch
        layer = build_rest_layer(n.panel_w, n.panel_h, self.cfg.get('rest_minutes', 90), self.px)
        n.prepare(layer, min(self.px(PANEL_R), layer.height // 2))
        n.show_alert(layer, REST_SHOW, bar=True)

    # -- 循环
    def loop(self):
        try:
            ms = self.notch.step()
        except Exception:
            ms = 200
        self.root.after(ms, self.loop)

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
                    _, kind, i, res = ev
                    self.state[(kind, i)] = res
                    if res[0] == 'ok':
                        self.data[(kind, i)] = res[1]
                        accs = self.cfg['accounts'] if kind == 'claude' else self.cfg['codex_accounts']
                        changed = False
                        for f in ('email', 'account_uuid', 'org_uuid', 'account_id'):
                            if res[1].get(f) and accs[i].get(f) != res[1][f]:
                                accs[i][f] = res[1][f]
                                changed = True
                        if changed:
                            self.save()
                            self.token_worker.wake.set()  # 账号身份更新了，重新归属一次
                    self.data_version += 1
                elif ev[0] == 'drag_start':
                    hwnd = ev[1]
                    if self.hangs.eligible(hwnd, os.getpid()):
                        self.notch.drag = {'hwnd': hwnd, 'placement': wh.get_placement(hwnd),
                                           'topmost': wh.is_topmost(hwnd)}
                elif ev[0] == 'drag_end':
                    d = self.notch.drag
                    if d and d['hwnd'] == ev[1]:
                        kind = self.notch.drop_kind
                        self.notch.drag = None
                        if kind == 'hot':
                            self.root.after(80, lambda d=d: self.do_hang(d))  # 等系统处理完松手（比如顶部吸附）
                        elif kind == 'full':
                            self.toast(f'最多悬挂 {wh.MAX_HANG} 个，先点右边 × 结束悬挂')
                elif ev[0] == 'clip':
                    self.notch.show_clip(ev[1])
                elif ev[0] == 'paste':
                    self.root.after(120, self.notch.dismiss_clip)  # 稍等一下，让粘贴先完成
                elif ev[0] == 'tokens':
                    self.tokens, self.tokens_ready = ev[1], True
                    self.data_version += 1
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def check_fullscreen(self):
        # 前台是全屏程序（看视频、玩游戏）时把刘海藏起来
        fg = user32.GetAncestor(user32.GetForegroundWindow(), tf.GA_ROOT)
        cls = tf.class_name(fg) if fg else ''
        full = bool(fg) and cls not in ('Progman', 'WorkerW') and tf.is_fullscreen(fg) and fg != self.notch.hwnd
        if full:
            r = wt.RECT()
            user32.GetWindowRect(fg, ctypes.byref(r))
            full = (r.left, r.top) == self.notch.mon[:2]
        self.notch.set_hidden(full)
        self.root.after(1000, self.check_fullscreen)

    def save(self):
        try:
            with open(tf.CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def set_clip_scale(self, f):
        self.cfg['clip_scale'] = f  # 下一次复制时生效
        self.save()

    def quit(self):
        self.save()
        self.hangs.release_all()  # 退出前把悬挂的窗口都还原
        self.notch.hires(False)
        self.root.destroy()


def main():
    if '--token-stats' in sys.argv:  # 打包后的 exe 被当作 token 统计子进程启动
        ts.serve()
        return
    kernel32.CreateMutexW(None, False, 'TimezoneNotch_SingleInstance')
    if ctypes.get_last_error() == 183:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        user32.SetProcessDPIAware()
    try:
        app = NotchApp()
        app.root.report_callback_exception = lambda *exc: log_error(*exc)  # 界面回调里的错误也记下来
        app.root.mainloop()
    except Exception:
        log_error(*sys.exc_info())
        raise


def log_error(etype, value, tb):
    r"""没有控制台窗口，出错写到 %APPDATA%\WindowsAI-Island\error.log，方便排查。"""
    import traceback
    try:
        with open(os.path.join(tf.DATA_DIR, 'error.log'), 'a', encoding='utf-8') as f:
            f.write(f'---- {datetime.now():%Y-%m-%d %H:%M:%S}\n')
            f.write(''.join(traceback.format_exception(etype, value, tb)))
    except OSError:
        pass


if __name__ == '__main__':
    main()
