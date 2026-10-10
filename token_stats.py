# -*- coding: utf-8 -*-
"""从本机日志统计每个 Claude / Codex 账号用了多少 token。

Claude：~/.claude/projects 下的会话记录（WSL 正在运行时也读 Linux 里的）。每条回复带 usage；
        账号归属依次看：记录里的 ownerAccountUuid → 桌面版按账号存的会话列表 → 入口（命令行/VS Code 用命令行登录的账号）
        → 时间上最近的已知会话。桌面版“第三方模式”(claude-desktop-3p) 不算任何账号。
Codex： ~/.codex/sessions、archived_sessions 下的 rollout 记录；账号看 state_5.sqlite 里 threads.creator_account_id，
        老会话没记账号的，按时间归给当时登录的账号。

日志只会往后追加，所以每个文件记住读到哪儿，下次只读新增的部分。
"""
import glob
import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta

CLAUDE_DIRS = [os.path.expanduser('~/.claude/projects')]
CODEX_HOME = os.path.expanduser('~/.codex')
DESKTOP_SESSIONS = os.path.expandvars(r'%APPDATA%\Claude\claude-code-sessions')


def _local_day(ts):
    """ISO 时间戳 → 本地日期字符串。"""
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d')
    except (ValueError, AttributeError):
        return None


def _read_new_lines(state, path):
    """读文件新增的完整行。文件变短（被重写）返回 None。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size < state.get('offset', 0):
        return None
    if size == state.get('offset', 0):
        return []
    with open(path, 'rb') as f:
        f.seek(state.get('offset', 0))
        data = f.read()
    end = data.rfind(b'\n') + 1  # 最后半行等下次再读
    state['offset'] = state.get('offset', 0) + end
    return data[:end].splitlines()


def _wsl_claude_dirs():
    """正在运行的 WSL 发行版里各用户的 ~/.claude/projects。没在运行的不碰（不会为此启动 WSL）。"""
    try:
        out = subprocess.run(['wsl.exe', '-l', '--running', '-q'], capture_output=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except Exception:
        return []
    dirs = []
    for distro in out.decode('utf-16-le', 'ignore').replace('\x00', '').split():
        base = rf'\\wsl.localhost\{distro}'
        dirs += glob.glob(base + r'\home\*\.claude\projects') + glob.glob(base + r'\root\.claude\projects')
    return dirs


class TokenStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.claude_files = {}   # path -> {'offset', 'sessions': {sid: {...}}}
        self.claude_seen = set()  # (message.id, requestId) 去重：流式分块、分叉会话都会重复写同一条回复
        self.codex_files = {}    # path -> {'offset', 'model', 'last_total', 'rec': {day: {model: [...]}}, 'cnt': {...}}
        self.codex_seen = set()  # response_id 去重

    # ------------------------------------------------------------ Claude
    def _scan_claude(self):
        dirs = list(CLAUDE_DIRS) + _wsl_claude_dirs()
        files = []
        for d in dirs:
            files += glob.glob(os.path.join(d, '**', '*.jsonl'), recursive=True)
        files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        for path in files:
            st = self.claude_files.setdefault(path, {'offset': 0, 'sessions': {}})
            lines = _read_new_lines(st, path)
            if lines is None:
                return False
            for raw in lines:
                if b'"usage"' not in raw and b'ownerAccountUuid' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                sid = d.get('sessionId')
                if not sid:
                    continue
                s = st['sessions'].setdefault(sid, {'entry': None, 'owner': None, 'org': None, 'first': None,
                                                    'days': {}})
                if d.get('ownerAccountUuid'):
                    s['owner'], s['org'] = d['ownerAccountUuid'], d.get('ownerOrganizationUuid')
                if d.get('entrypoint'):
                    s['entry'] = d['entrypoint']
                if d.get('type') != 'assistant':
                    continue
                m = d.get('message') or {}
                u = m.get('usage')
                if not u:
                    continue
                key = (m.get('id'), d.get('requestId'))
                if key in self.claude_seen:
                    continue
                self.claude_seen.add(key)
                day = _local_day(d.get('timestamp'))
                if not day:
                    continue
                s['first'] = min(s['first'] or d['timestamp'], d['timestamp'])
                v = s['days'].setdefault(day, {}).setdefault(m.get('model') or 'unknown', [0, 0, 0, 0])
                v[0] += u.get('input_tokens') or 0
                v[1] += u.get('output_tokens') or 0
                v[2] += u.get('cache_creation_input_tokens') or 0
                v[3] += u.get('cache_read_input_tokens') or 0
        return True

    @staticmethod
    def _desktop_owners():
        """桌面版按账号存的会话列表：会话 id -> 账号 uuid。同一会话在多个账号下（之前合并过）时取旧账号。"""
        found = defaultdict(set)
        latest = {}
        for path in glob.glob(os.path.join(DESKTOP_SESSIONS, '*', '*', 'local_*.json')):
            acct = os.path.basename(os.path.dirname(os.path.dirname(path)))
            try:
                with open(path, encoding='utf-8') as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if d.get('cliSessionId'):
                found[d['cliSessionId']].add(acct)
            latest[acct] = max(latest.get(acct, 0), d.get('lastActivityAt') or 0)
        current = max(latest, key=latest.get) if latest else None
        out = {}
        for sid, accts in found.items():
            others = accts - {current}
            out[sid] = sorted(others)[0] if others else current
        return out

    def _claude_by_account(self, accounts):
        """accounts: 配置里的 Claude 账号列表（含 account_uuid / org_uuid / dir）。返回 {序号: {day: {model: [..]}}}。"""
        by_uuid = {a.get('account_uuid'): i for i, a in enumerate(accounts) if a.get('account_uuid')}
        by_org = {a.get('org_uuid'): i for i, a in enumerate(accounts) if a.get('org_uuid')}
        cli_idx = next((i for i, a in enumerate(accounts) if os.path.expanduser(a['dir']) ==
                        os.path.expanduser('~/.claude')), 0)
        desktop = self._desktop_owners()
        # 先合并同一会话分散在多个文件（子代理、WSL）里的信息
        sess = {}
        for st in self.claude_files.values():
            for sid, s in st['sessions'].items():
                t = sess.setdefault(sid, {'entry': None, 'owner': None, 'org': None, 'first': None, 'days': []})
                t['entry'] = t['entry'] or s['entry']
                t['owner'] = t['owner'] or s['owner']
                t['org'] = t['org'] or s['org']
                if s['first'] and (not t['first'] or s['first'] < t['first']):
                    t['first'] = s['first']
                t['days'].append(s['days'])
        known, unknown = {}, []
        for sid, t in sess.items():
            if t['entry'] == 'claude-desktop-3p':
                continue  # 第三方模式，不是订阅账号
            idx = None
            if t['owner']:
                idx = by_uuid.get(t['owner'], by_org.get(t['org']))
            elif sid in desktop:
                idx = by_uuid.get(desktop[sid])
            elif t['entry'] and not t['entry'].startswith('claude-desktop'):
                idx = cli_idx
            if idx is None:
                unknown.append(sid)
            else:
                known[sid] = (idx, False)
        # 剩下的桌面版会话：归给时间上最近的已知会话的账号
        ref = sorted((sess[s]['first'], i) for s, (i, _) in known.items() if sess[s]['first'])
        for sid in unknown:
            f = sess[sid]['first'] or ''
            before = [i for ts, i in ref if ts <= f]
            known[sid] = (before[-1] if before else (ref[0][1] if ref else cli_idx), True)
        out = defaultdict(lambda: {'days': defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0])), 'inferred': 0, 'all': 0})
        for sid, (idx, inferred) in known.items():
            if idx >= len(accounts):
                continue
            o = out[idx]
            for days in sess[sid]['days']:
                for day, models in days.items():
                    for model, v in models.items():
                        acc = o['days'][day][model]
                        for k in range(4):
                            acc[k] += v[k]
                        n = sum(v)
                        o['all'] += n
                        if inferred:
                            o['inferred'] += n
        return out

    # ------------------------------------------------------------ Codex
    def _scan_codex(self):
        files = glob.glob(os.path.join(CODEX_HOME, 'sessions', '**', '*.jsonl'), recursive=True) + \
            glob.glob(os.path.join(CODEX_HOME, 'archived_sessions', '**', '*.jsonl'), recursive=True)
        # 归档时文件会从 sessions 挪到 archived_sessions：按线程 id 认文件，挪了也不重复算
        by_tid = {}
        for path in files:
            by_tid.setdefault(os.path.basename(path)[-42:-6], path)
        for tid, path in by_tid.items():
            st = self.codex_files.get(tid)
            if st is None or st['path'] != path:  # 新文件，或被挪到归档目录：从头读新位置
                if st:
                    self.codex_seen -= st.get('rids', set())
                st = self.codex_files[tid] = {'path': path, 'offset': 0, 'model': 'unknown', 'last_total': None,
                                              'rec': {}, 'cnt': {}, 'first': None, 'rids': set()}
            lines = _read_new_lines(st, path)
            if lines is None:
                return False
            for raw in lines:
                if b'"turn_context"' in raw:
                    try:
                        st['model'] = json.loads(raw)['payload'].get('model') or st['model']
                    except (ValueError, KeyError):
                        pass
                    continue
                is_rec = b'token_usage_record' in raw
                if not is_rec and b'"token_count"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                day = _local_day(d.get('timestamp'))
                if not day:
                    continue
                st['first'] = st['first'] or d.get('timestamp')
                p = d.get('payload') or {}
                if is_rec:
                    rid = p.get('response_id')
                    if rid in self.codex_seen:
                        continue
                    self.codex_seen.add(rid)
                    st['rids'].add(rid)
                    u = p.get('usage') or {}
                    bucket = st['rec']
                else:
                    info = p.get('info') or {}
                    tot = info.get('total_token_usage')
                    if not tot:
                        continue
                    prev = st['last_total']
                    st['last_total'] = tot
                    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
                    if prev is None:
                        u = tot
                    else:
                        u = {k: (tot.get(k) or 0) - (prev.get(k) or 0) for k in keys}
                        if all(v == 0 for v in u.values()):
                            continue  # 只是限额刷新，没有新用量
                        if any(v < 0 for v in u.values()):  # 压缩后计数器重置
                            u = info.get('last_token_usage') or {}
                    bucket = st['cnt']
                v = bucket.setdefault(day, {}).setdefault(st['model'], [0, 0, 0, 0])
                v[0] += u.get('input_tokens') or 0          # 含缓存命中部分
                v[1] += u.get('output_tokens') or 0
                v[2] += u.get('cached_input_tokens') or 0
                v[3] += u.get('reasoning_output_tokens') or 0
        return True

    @staticmethod
    def _codex_threads():
        db = os.path.join(CODEX_HOME, 'state_5.sqlite')
        try:
            c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)
            rows = c.execute('select id, creator_account_id, model_provider, created_at from threads').fetchall()
            c.close()
        except sqlite3.Error:
            return {}
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def _codex_by_account(self, accounts):
        """accounts: [{'account_id': ...}, ...]。返回 {序号: {...}}。"""
        by_id = {a.get('account_id'): i for i, a in enumerate(accounts) if a.get('account_id')}
        threads = self._codex_threads()
        ref = sorted((t[2], t[0]) for t in threads.values() if t[0] and t[2])
        out = defaultdict(lambda: {'days': defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0])), 'inferred': 0, 'all': 0})
        for tid, st in self.codex_files.items():
            acct, provider, created = threads.get(tid, (None, 'openai', None))
            if provider and provider != 'openai':
                continue  # 第三方 API，不是 ChatGPT 账号
            inferred = False
            if not acct:  # 老会话没记账号：归给当时最近登录的账号
                if created is None and st['first']:
                    created = datetime.fromisoformat(st['first'].replace('Z', '+00:00')).timestamp()
                before = [a for ts, a in ref if created and ts <= created]
                acct = before[-1] if before else (ref[0][1] if ref else None)
                inferred = True
            idx = by_id.get(acct)
            if idx is None:
                continue
            o = out[idx]
            src = st['rec'] if st['rec'] else st['cnt']  # 新日志有逐条记录，老日志用累计数相减
            for day, models in src.items():
                for model, v in models.items():
                    acc = o['days'][day][model]
                    for k in range(4):
                        acc[k] += v[k]
                    n = v[0] + v[1]
                    o['all'] += n
                    if inferred:
                        o['inferred'] += n
        return out

    # ------------------------------------------------------------ 汇总
    def update(self, claude_accounts, codex_accounts):
        """增量扫描并按账号汇总。返回 {('claude'|'codex', 序号): 统计}。"""
        if not (self._scan_claude() and self._scan_codex()):
            self.reset()  # 有文件被重写，全部重来
            self._scan_claude()
            self._scan_codex()
        result = {}
        for kind, agg in (('claude', self._claude_by_account(claude_accounts)),
                          ('codex', self._codex_by_account(codex_accounts))):
            for idx, o in agg.items():
                result[(kind, idx)] = summarize(kind, o)
        return result


def summarize(kind, o):
    """按时间段汇总：今天、近 7 天、本月、累计；外加各模型占比。"""
    today = datetime.now().date()
    week_from = (today - timedelta(days=6)).strftime('%Y-%m-%d')
    month_from = today.replace(day=1).strftime('%Y-%m-%d')
    today_s = today.strftime('%Y-%m-%d')
    periods = {'today': [0, 0], 'week': [0, 0], 'month': [0, 0], 'all': [0, 0]}  # [总量, 输出]
    models = defaultdict(int)
    for day, ms in o['days'].items():
        for model, v in ms.items():
            total = sum(v) if kind == 'claude' else v[0] + v[1]
            out = v[1]
            hits = ['all'] + (['month'] if day >= month_from else []) + (['week'] if day >= week_from else []) + \
                   (['today'] if day == today_s else [])
            for h in hits:
                periods[h][0] += total
                periods[h][1] += out
            models[model] += total
    top = sorted(models.items(), key=lambda x: -x[1])
    return {'periods': periods, 'models': top, 'inferred': o['inferred'] / o['all'] if o['all'] else 0}


def serve():
    """作为子进程运行：每从 stdin 收到一行账号配置，就增量扫描一次，把结果写一行 JSON 到 stdout。
    放在独立进程里，扫描大文件时不会抢界面线程的 GIL，动画不卡。"""
    import sys
    stats = TokenStats()
    for line in sys.stdin:
        try:
            req = json.loads(line)
            res = stats.update(req['claude'], req['codex'])
            out = {f'{k}:{i}': v for (k, i), v in res.items()}
        except Exception as e:
            out = {'error': str(e)}
        sys.stdout.write(json.dumps(out) + '\n')
        sys.stdout.flush()


def fmt_tokens(n):
    if n >= 1e8:
        return f'{n / 1e8:.1f}亿'
    if n >= 1e4:
        return f'{n / 1e4:.0f}万' if n >= 1e6 else f'{n / 1e4:.1f}万'
    return f'{n:,}'


if __name__ == "__main__":
    serve()
