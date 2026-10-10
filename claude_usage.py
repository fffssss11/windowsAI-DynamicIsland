# -*- coding: utf-8 -*-
"""读取 Claude 账号的 5 小时 / 每周用量。

凭据用 Claude Code 登录后留下的 <配置目录>/.credentials.json；
access token 过期时用 refresh token 换新的，并写回同一个文件（和 Claude Code 自己的做法一样）。
"""
import glob
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

API = 'https://api.anthropic.com'
TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'  # Claude Code 的 OAuth client
DEFAULT_SCOPES = ['user:profile', 'user:inference', 'user:sessions:claude_code', 'user:mcp_servers']
HEADERS = {'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/2.1 (timezone-float)',
           'Content-Type': 'application/json'}

_file_lock = threading.Lock()


class NotLoggedIn(Exception):
    pass


class AuthExpired(Exception):
    pass


def cred_path(config_dir):
    return os.path.join(os.path.expanduser(config_dir), '.credentials.json')


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        raise NotLoggedIn
    o = d.get('claudeAiOauth') or {}
    if not o.get('accessToken'):
        raise NotLoggedIn
    return d, o


def _request(url, token=None, body=None, timeout=15):
    headers = dict(HEADERS)
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _refresh(path, stale_token):
    """用 refresh token 换新 token 并写回文件。
    如果文件里的 token 已经不是 stale_token（Claude Code 刚换过），直接用文件里的。"""
    with _file_lock:
        d, o = _read(path)
        if o['accessToken'] != stale_token:
            return o['accessToken']
        if not o.get('refreshToken'):
            raise AuthExpired
        try:
            r = _request(TOKEN_URL, body={'grant_type': 'refresh_token', 'refresh_token': o['refreshToken'],
                                          'client_id': CLIENT_ID,
                                          'scope': ' '.join(o.get('scopes') or DEFAULT_SCOPES)})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise AuthExpired
            raise
        d, o2 = _read(path)  # 重新读，保留这期间别人写进去的其它字段
        if o2['accessToken'] != stale_token:
            return o2['accessToken']
        o2['accessToken'] = r['access_token']
        if r.get('refresh_token'):
            o2['refreshToken'] = r['refresh_token']
        o2['expiresAt'] = int(time.time() * 1000 + r.get('expires_in', 28800) * 1000)
        if r.get('scope'):
            o2['scopes'] = r['scope'].split()
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)
        return o2['accessToken']


def _get(path, url):
    _, o = _read(path)
    token = o['accessToken']
    if o.get('expiresAt', 0) < time.time() * 1000 + 60_000:
        token = _refresh(path, token)
    try:
        return _request(url, token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    # 401：token 被提前作废了，强制换一次再试
    return _request(url, _refresh(path, token))


def _parse_window(w):
    if not w or w.get('utilization') is None:
        return None
    reset = w.get('resets_at')
    return {'used': float(w['utilization']),
            'resets_at': datetime.fromisoformat(reset) if reset else None}


def fetch(config_dir, want_email=True):
    """返回 {'email','plan','five_hour','seven_day','time'}；未登录 / 登录失效抛 NotLoggedIn / AuthExpired。"""
    path = cred_path(config_dir)
    _, o = _read(path)
    u = _get(path, API + '/api/oauth/usage')
    email = account_uuid = org_uuid = None
    if want_email:
        try:
            prof = _get(path, API + '/api/oauth/profile')
            email = (prof.get('account') or {}).get('email')
            account_uuid = (prof.get('account') or {}).get('uuid')
            org_uuid = (prof.get('organization') or {}).get('uuid')
        except Exception:
            pass
    return {'email': email, 'account_uuid': account_uuid, 'org_uuid': org_uuid,
            'plan': (o.get('subscriptionType') or '').capitalize(),
            'five_hour': _parse_window(u.get('five_hour')),
            'seven_day': _parse_window(u.get('seven_day')), 'time': time.time()}


def find_claude_exe():
    """优先用桌面版自带的最新 Claude Code，其次 PATH 里的。"""
    cands = glob.glob(os.path.join(os.path.expandvars(r'%APPDATA%\Claude\claude-code'), '*', '*', 'claude.exe'))

    def ver(p):
        try:
            return tuple(int(x) for x in os.path.basename(os.path.dirname(os.path.dirname(p))).split('.'))
        except ValueError:
            return (0,)
    return max(cands, key=ver) if cands else shutil.which('claude')


def open_login(config_dir, email=None):
    """新开一个命令行窗口，在指定配置目录里登录 Claude（浏览器里完成授权）。"""
    exe = find_claude_exe()
    if not exe:
        raise FileNotFoundError('没找到 claude.exe')
    os.makedirs(os.path.expanduser(config_dir), exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not (k.startswith('CLAUDE_CODE_') or k == 'CLAUDECODE')}
    env['CLAUDE_CONFIG_DIR'] = os.path.expanduser(config_dir)
    args = [exe, 'auth', 'login'] + (['--email', email] if email else [])
    subprocess.Popen(['cmd', '/k'] + args, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
