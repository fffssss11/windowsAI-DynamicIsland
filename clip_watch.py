# -*- coding: utf-8 -*-
"""监听复制 / 截图，以及粘贴动作。

- ClipWatcher：看剪贴板序号变没变（很便宜），变了就读出内容（图片 / 文字 / 文件）发给界面。
  划词换算自己发 Ctrl+C 造成的变化会跳过；密码管理器等标了“别给剪贴板监视器看”的内容只提示、不显示。
- PasteWatcher：有内容挂着时，盯 Ctrl+V / Shift+Insert，按下就通知界面收起。
"""
import ctypes
import os
import threading
import time

from PIL import Image, ImageGrab

import timezone_float as tf

user32, kernel32 = tf.user32, tf.kernel32
CF_UNICODETEXT, CF_HDROP, CF_BITMAP, CF_DIB, CF_DIBV5 = 13, 15, 2, 8, 17
VK_V, VK_INSERT = 0x56, 0x2D
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
MAX_IMAGE_SIDE = 1600  # 读进来的图先缩到这么大，够做缩略图了（灵动岛放大后缩略图也大）


def _formats():
    """当前剪贴板里所有格式 id 和是否敏感。需要先打开剪贴板。"""
    fmts, sensitive = [], False
    f = 0
    while True:
        f = user32.EnumClipboardFormats(f)
        if not f:
            break
        fmts.append(f)
        name = tf.clip_format_name(f)
        if name in ('ExcludeClipboardContentFromMonitorProcessing', 'Clipboard Viewer Ignore'):
            sensitive = True
        elif name == 'CanIncludeInClipboardHistory':
            h = user32.GetClipboardData(f)
            p = kernel32.GlobalLock(h) if h else None
            if p:
                try:
                    if ctypes.c_uint32.from_address(p).value == 0:
                        sensitive = True
                finally:
                    kernel32.GlobalUnlock(h)
    return fmts, sensitive


def read_clip():
    """读剪贴板，返回 {'kind': 'image'|'text'|'files'|'hidden', ...}；读不到或不认识返回 None。"""
    if not tf.open_clipboard():
        return None
    try:
        fmts, sensitive = _formats()
    finally:
        user32.CloseClipboard()
    if not fmts:
        return None
    if sensitive:
        return {'kind': 'hidden'}
    if CF_HDROP in fmts:
        files = ImageGrab.grabclipboard()
        if isinstance(files, list) and files:
            out = {'kind': 'files', 'files': files}
            if len(files) == 1 and os.path.splitext(files[0])[1].lower() in IMAGE_EXT:
                try:
                    with Image.open(files[0]) as im:
                        out['size'] = im.size
                        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
                        out['image'] = im.convert('RGBA')
                except Exception:
                    pass
            return out
    if CF_UNICODETEXT in fmts:  # Office 复制表格时文字和图片都有，优先当文字
        text = tf.clipboard_text()
        if text and text.strip():
            return {'kind': 'text', 'text': text}
    png = any(tf.clip_format_name(f) == 'PNG' for f in fmts)
    if CF_DIB in fmts or CF_DIBV5 in fmts or CF_BITMAP in fmts or png:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            size = img.size
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            return {'kind': 'image', 'image': img.convert('RGBA'), 'size': size}
    return None


class ClipWatcher(threading.Thread):
    def __init__(self, app, on_clip):
        super().__init__(daemon=True)
        self.app = app
        self.on_clip = on_clip  # 在本线程里调用：on_clip(内容)
        self.last = user32.GetClipboardSequenceNumber()  # 启动前就有的内容不提示

    def run(self):
        while True:
            time.sleep(0.1)
            seq = user32.GetClipboardSequenceNumber()
            if seq == self.last:
                continue
            g = tf.CLIP_GUARD
            if g['busy']:
                continue  # 划词换算正在借用剪贴板，等它做完再判断
            if g['seq'] is not None and seq == g['seq']:
                self.last = seq  # 划词借用后又原样还回去了，不算复制
                continue
            self.last = seq
            if not self.app.cfg.get('clip_popup', True):
                continue
            time.sleep(0.05)  # 等复制方把所有格式写完
            if user32.GetClipboardSequenceNumber() != seq:
                continue  # 又变了，下一轮再读
            try:
                clip = read_clip()
            except Exception:
                clip = None
            if clip:
                self.on_clip(clip)


class PasteWatcher(threading.Thread):
    """有复制内容挂在灵动岛上时，看用户有没有按 Ctrl+V / Shift+Insert。"""

    def __init__(self, app, on_paste):
        super().__init__(daemon=True)
        self.app = app
        self.on_paste = on_paste

    def run(self):
        prev_v = prev_ins = False
        while True:
            if not self.app.clip_pinned:
                prev_v = prev_ins = False
                time.sleep(0.08)
                continue
            v, ins = tf.key_down(VK_V), tf.key_down(VK_INSERT)
            ctrl, shift = tf.key_down(tf.VK_CONTROL), tf.key_down(tf.VK_SHIFT)
            if (v and not prev_v and ctrl) or (ins and not prev_ins and shift):
                self.on_paste()
            prev_v, prev_ins = v, ins
            time.sleep(0.01)
