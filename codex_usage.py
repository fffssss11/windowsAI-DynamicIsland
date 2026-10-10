# -*- coding: utf-8 -*-
"""读取 Codex（ChatGPT 账号登录）的 5 小时 / 每周用量。

凭据是 Codex 登录后的 auth.json；access token 过期时用 refresh token 换新的并写回同一个文件。
"""
import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from claude_usage import AuthExpired, NotLoggedIn

USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
TOKEN_URL = 'https://auth.openai.com/oauth/token'
CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'  # Codex CLI 的 OAuth client
UA = 'codex_cli_rs/0.130.0 (timezone-float)'

_file_lock = threading.Lock()


def jwt_claims(token):
    try:
        p = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))
    except Exception:
        return {}


def _read(path):
    try:
        with open(os.path.expanduser(path), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        raise NotLoggedIn
    t = d.get('tokens') or {}
    if not t.get('access_token'):
        raise NotLoggedIn
    return d, t


def identity(path):
    """不联网，直接从 id_token 里读邮箱、套餐、账号 id。"""
    _, t = _read(path)
    c = jwt_claims(t.get('id_token', ''))
    auth = c.get('https://api.openai.com/auth', {})
    return {'email': c.get('email'), 'plan': auth.get('chatgpt_plan_type') or '',
            'account_id': t.get('account_id') or auth.get('chatgpt_account_id')}


def _post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _refresh(path, stale_token):
    path = os.path.expanduser(path)
    with _file_lock:
        d, t = _read(path)
        if t['access_token'] != stale_token:
            return t['access_token']  # Codex 自己刚换过
        if not t.get('refresh_token'):
            raise AuthExpired
        try:
            r = _post_json(TOKEN_URL, {'client_id': CLIENT_ID, 'grant_type': 'refresh_token',
                                       'refresh_token': t['refresh_token'], 'scope': 'openid profile email'})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise AuthExpired
            raise
        d, t = _read(path)
        if t['access_token'] != stale_token:
            return t['access_token']
        for k in ('id_token', 'access_token', 'refresh_token'):
            if r.get(k):
                t[k] = r[k]
        d['last_refresh'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)
        return t['access_token']


def _get_usage(path):
    _, t = _read(path)
    token = t['access_token']
    if jwt_claims(token).get('exp', 0) < time.time() + 60:
        token = _refresh(path, token)
    for attempt in range(2):
        req = urllib.request.Request(USAGE_URL, headers={
            'Authorization': 'Bearer ' + token, 'ChatGPT-Account-Id': t.get('account_id') or '',
            'User-Agent': UA, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt:
                if e.code in (401, 403):
                    raise AuthExpired
                raise
            token = _refresh(path, token)


def _window(w):
    if not w or w.get('used_percent') is None:
        return None
    if w.get('reset_at'):
        at = datetime.fromtimestamp(w['reset_at'], timezone.utc)
    elif w.get('reset_after_seconds') is not None:
        at = datetime.now(timezone.utc) + timedelta(seconds=w['reset_after_seconds'])
    else:
        at = None
    return {'used': float(w['used_percent']), 'resets_at': at, 'seconds': w.get('limit_window_seconds')}


def fetch(path):
    """返回 {'email','plan','five_hour','seven_day','time'}，结构和 claude_usage.fetch 一样。"""
    ident = identity(path)
    u = _get_usage(path)
    rl = u.get('rate_limit') or {}
    wins = [x for x in (_window(rl.get('primary_window')), _window(rl.get('secondary_window'))) if x]
    # 按窗口长度分：短的是 5 小时，长的是每周
    wins.sort(key=lambda w: w['seconds'] or 0)
    five = next((w for w in wins if (w['seconds'] or 0) <= 6 * 3600), None)
    week = next((w for w in wins if (w['seconds'] or 0) > 6 * 3600), None)
    plan = u.get('plan_type') or ident['plan']
    return {'email': ident['email'], 'account_id': ident['account_id'], 'plan': plan, 'five_hour': five, 'seven_day': week,
            'limit_reached': bool(rl.get('limit_reached')), 'time': time.time()}


def account_id_for_email(email):
    """没登录的账号：从 Codex 账号切换器的 registry.json 里按邮箱找账号 id（用来认领本机日志）。"""
    try:
        with open(os.path.expanduser('~/.codex/accounts/registry.json'), encoding='utf-8') as f:
            for a in json.load(f).get('accounts', []):
                if email and a.get('email') == email:
                    return a.get('chatgpt_account_id')
    except (OSError, ValueError):
        pass
    return None


def find_codex_exe():
    p = os.path.expandvars(r'%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe')
    if os.path.exists(p):
        return p
    import shutil
    return shutil.which('codex')


def open_login(codex_home):
    """新开命令行窗口，在单独的 CODEX_HOME 里登录（浏览器里授权）。"""
    exe = find_codex_exe()
    if not exe:
        raise FileNotFoundError('没找到 codex.exe')
    home = os.path.expanduser(codex_home)
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, CODEX_HOME=home)
    # 新目录里没有 .env，把默认 ~/.codex/.env 里的代理设置带过去（国内登录要走代理）
    try:
        with open(os.path.expanduser('~/.codex/.env'), encoding='utf-8') as f:
            for line in f:
                k, sep, v = line.strip().partition('=')
                if sep and k.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
                    env.setdefault(k, v)
    except OSError:
        pass
    # 凭据存成 auth.json 文件（不进系统钥匙串），刘海才读得到
    subprocess.Popen(['cmd', '/k', exe, 'login', '-c', 'cli_auth_credentials_store="file"'], env=env,
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
