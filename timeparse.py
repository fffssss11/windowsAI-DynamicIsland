# -*- coding: utf-8 -*-
"""从一段文字里识别时间（中英文都行），并换算到洛杉矶 / 纽约 / 北京。"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONES = {  # key: (显示名, IANA 时区)
    'la': ('洛杉矶', 'America/Los_Angeles'),
    'ny': ('纽约', 'America/New_York'),
    'cn': ('北京', 'Asia/Shanghai'),
}
ORDER = ['la', 'ny', 'cn']
WEEK_CN = '一二三四五六日'
MAX_LEN = 400


_B = r'(?:(?<![A-Za-z0-9])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?![A-Za-z0-9]))'


def _rx(pattern, flags=0):
    r"""编译正则。把 \b 换成只看英文字母数字的边界：汉字在 re 里也算单词字符，\b 贴着中文（如“PT时间”）会失效。"""
    return re.compile(pattern.replace(r'\b', _B), flags)


def get_tz(key):
    if key in ZONES:
        return ZoneInfo(ZONES[key][1])
    if key == 'utc':
        return timezone.utc
    return timezone(timedelta(minutes=int(key[3:])))  # 'utc+330' 这种


def zone_label(key):
    if key in ZONES:
        return ZONES[key][0]
    if key == 'utc':
        return 'UTC'
    minutes = int(key[3:])
    sign = '+' if minutes >= 0 else '-'
    h, m = divmod(abs(minutes), 60)
    return f'UTC{sign}{h}' + (f':{m:02d}' if m else '')


def abbr(dt, key):
    """美国时区显示 PDT/EST 这类缩写，其它显示 UTC 偏移。"""
    if key in ('la', 'ny'):
        return dt.tzname()
    off = int(dt.utcoffset().total_seconds() // 60)
    return zone_label('utc' if off == 0 else f'utc{off:+d}')


def fmt_date(dt):
    return f'{dt.month}月{dt.day}日 周{WEEK_CN[dt.weekday()]}'


# ---------------------------------------------------------------- 时区关键词
_ZONE_RX = [
    ('off', _rx(r'(?:UTC|GMT)\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?')),
    ('cn', _rx(r'东八区|東八區|北京|中国|中國|国内|國內|上海|深圳|广州|廣州|杭州|香港|台北|新加坡'
                      r'|\bBJT\b|\bBeijing\b|\bChina\b|\bShanghai\b')),
    ('utc', _rx(r'\bUTC\b|\bGMT\b|\bZulu\b|世界标准时间|格林尼治')),
    ('la', _rx(r'洛杉矶|洛杉磯|美西|太平洋|加州|旧金山|舊金山|西雅图|硅谷|湾区'
                      r'|\bP[SD]?T\b|\bPacific\b|\bLos Angeles\b|\bSan Francisco\b|\bSeattle\b|\bL\.?A\b\.?')),
    ('ny', _rx(r'纽约|紐約|美东|美東|东部|東部|华盛顿|波士顿'
                      r'|\bE[SD]?T\b|\bEastern\b|\bNew York\b|\bNYC?\b|\bBoston\b')),
]


def _overlaps(span, taken):
    return any(span[0] < e and s < span[1] for s, e in taken)


def find_zones(text):
    found, taken = [], []
    for key, rx in _ZONE_RX:
        for m in rx.finditer(text):
            if _overlaps(m.span(), taken):
                continue
            k = key
            if key == 'off':
                sign = -1 if m.group(1) in '-−' else 1
                minutes = sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))
                k = 'cn' if minutes == 480 else ('utc' if minutes == 0 else f'utc{minutes:+d}')
            found.append((m.start(), m.end(), k))
            taken.append(m.span())
    return sorted(found)


def pick_zone(start, end, zones, default_key):
    """离这个时间最近（12 字以内）的时区词；没有就看全文是否只提到一个时区；再没有用默认。"""
    best = None
    for s, e, k in zones:
        if s >= end:
            d = s - end
        elif e <= start:
            d = start - e + 0.5  # 同距离时优先取后面的，比如 “2pm PT”
        else:
            d = 0
        if d <= 12 and (best is None or d < best[0]):
            best = (d, k)
    if best:
        return best[1], True
    keys = {k for _, _, k in zones}
    if len(keys) == 1:
        return keys.pop(), True
    return default_key, False


# ---------------------------------------------------------------- 时间
PERIODS = {
    '凌晨': 'dawn', '清晨': 'am', '早晨': 'am', '早上': 'am', '上午': 'am', '今早': 'am', '明早': 'am', '早': 'am',
    '中午': 'noon', '正午': 'noon',
    '午后': 'pm', '下午': 'pm',
    '傍晚': 'eve', '晚上': 'eve', '晚间': 'eve', '今晚': 'eve', '明晚': 'eve', '昨晚': 'eve', '晚': 'eve',
    '夜里': 'eve', '夜间': 'eve', '半夜': 'eve', '深夜': 'eve', '午夜': 'eve',
}
_P_ALT = '|'.join(sorted(PERIODS, key=len, reverse=True))
_NUM = r'[零〇一二两兩三四五六七八九十\d]{1,3}'
_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '兩': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

_RX_EN_RANGE = _rx(r'(?<![\d:])(\d{1,2})(?:[:：](\d{2}))?\s*(?:-|–|—|~|～|to|until|till)\s*'
                          r'(\d{1,2})(?:[:：](\d{2}))?\s*([ap])\.?\s?m(?![a-z])\.?', re.I)
_RX_EN = _rx(r'(?<![\d:])(\d{1,2})(?:[:：](\d{2}))?\s*([ap])\.?\s?m(?![a-z])\.?', re.I)
_RX_NOON = _rx(r'\b(?:12\s*)?(noon|midnight)\b', re.I)
_RX_24 = _rx(rf'(?:({_P_ALT})\s*)?(?<![\d:：])(\d{{1,2}})[:：](\d{{2}})(?:[:：]\d{{2}})?(?!\d)')
_RX_CN = _rx(rf'(?:({_P_ALT})\s*)?({_NUM})\s*[点點时時](?:钟|鐘)?(?:\s*(半|一刻|三刻|整|{_NUM})(?:\s*分)?)?')
_RX_JOIN = _rx(r'^\s*(?:-|–|—|~|～|到|至|to)\s*$', re.I)


def cn2int(s):
    if s.isdigit():
        return int(s)
    if '十' in s:
        a, _, b = s.partition('十')
        tens = _CN_DIGITS.get(a) if a else 1
        ones = _CN_DIGITS.get(b) if b else 0
        return None if tens is None or ones is None else tens * 10 + ones
    v = 0
    for ch in s:
        if ch not in _CN_DIGITS:
            return None
        v = v * 10 + _CN_DIGITS[ch]
    return v


def _en_hour(h, ap):
    if not 1 <= h <= 12:
        return None
    if ap == 'a':
        return 0 if h == 12 else h
    return 12 if h == 12 else h + 12


def apply_period(h, p):
    if p is None or p == 'am':
        return h
    if p == 'dawn':
        return 0 if h == 12 else h
    if p == 'noon':
        return h + 12 if 1 <= h <= 10 else h            # 中午1点=13:00
    if p == 'pm':
        return h + 12 if 1 <= h <= 11 else h            # 下午3点=15:00
    # eve：晚上8点=20:00，晚上12点=次日0点，晚上1点=次日1点
    if 5 <= h <= 11:
        return h + 12
    if h == 12:
        return 24
    if 0 <= h <= 4:
        return 24 + h
    return h


def find_times(text):
    items, taken = [], []

    def add(span, whole, h, m, period=None, fixed=True):
        if _overlaps(whole, taken) or h is None or m is None or m >= 60:
            return
        items.append({'start': span[0], 'end': span[1], 'h': h, 'm': m, 'period': period, 'fixed': fixed})

    for mt in _RX_EN_RANGE.finditer(text):
        ap = mt.group(5).lower()
        h1, h2 = int(mt.group(1)), int(mt.group(3))
        ap1 = 'a' if ap == 'p' and h1 > h2 and h1 != 12 else ap  # 11-1pm → 11am-1pm
        add((mt.start(1), mt.end(2) if mt.group(2) else mt.end(1)), mt.span(), _en_hour(h1, ap1), int(mt.group(2) or 0))
        add((mt.start(3), mt.end()), mt.span(), _en_hour(h2, ap), int(mt.group(4) or 0))
        taken.append(mt.span())
    for mt in _RX_EN.finditer(text):
        add(mt.span(), mt.span(), _en_hour(int(mt.group(1)), mt.group(3).lower()), int(mt.group(2) or 0))
        taken.append(mt.span())
    for mt in _RX_NOON.finditer(text):
        add(mt.span(), mt.span(), 12 if mt.group(1).lower() == 'noon' else 0, 0)
        taken.append(mt.span())
    for mt in _RX_24.finditer(text):
        h = int(mt.group(2))
        if h <= 24:
            add(mt.span(), mt.span(), h, int(mt.group(3)), PERIODS.get(mt.group(1)), fixed=False)
            taken.append(mt.span())
    for mt in _RX_CN.finditer(text):
        if not mt.group(1) and mt.start(2) > 0 and text[mt.start(2) - 1] == '第':
            continue  # “第3点”
        if not mt.group(1) and not mt.group(3) and not mt.group(2).isdigit() and not re.search('[钟鐘]', mt.group(0)):
            continue  # 光秃秃的“两点”多半是“有两点要注意”，不当成时间
        h = cn2int(mt.group(2))
        mm = mt.group(3)
        if mm in (None, '整'):
            mi = 0
        else:
            mi = {'半': 30, '一刻': 15, '三刻': 45}.get(mm)
            if mi is None:
                mi = cn2int(mm)
        if h is not None and h <= 24:
            add(mt.span(), mt.span(), h, mi, PERIODS.get(mt.group(1)), fixed=False)
            taken.append(mt.span())

    items.sort(key=lambda t: t['start'])
    # “下午2点到4点”：后一个没写上下午，就沿用前一个
    for prev, cur in zip(items, items[1:]):
        if not cur['fixed'] and cur['period'] is None and prev['period'] \
                and _RX_JOIN.match(text[prev['end']:cur['start']]):
            cur['period'] = prev['period']
    for t in items:
        if not t['fixed']:
            t['h'] = apply_period(t['h'], t['period'])
    return [t for t in items if t['h'] < 24 or (t['h'] == 24 and t['m'] == 0) or t['period'] == 'eve']


# ---------------------------------------------------------------- 日期
_MON = (r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?'
        r'|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?')
_RX_YMD = _rx(r'(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)')
_RX_CN_MD = _rx(r'(?<!\d)(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号號]')
_RX_EN_MD = _rx(r'\b' + _MON + r'\s*(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4})\b)?', re.I)
_RX_EN_DM = _rx(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?' + _MON + r'(?:,?\s*(\d{4})\b)?', re.I)
_RX_NUM_MD = _rx(r'(?<![\d/.])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])')
_REL = [('大后天', 3), ('大後天', 3), ('后天', 2), ('後天', 2), ('明天', 1), ('明日', 1), ('明晚', 1), ('明早', 1),
        ('今天', 0), ('今日', 0), ('今晚', 0), ('今早', 0), ('昨天', -1), ('昨日', -1), ('昨晚', -1),
        ('tomorrow', 1), ('tonight', 0), ('today', 0), ('yesterday', -1)]
_RX_REL = _rx('|'.join(w for w, _ in _REL), re.I)
_RX_WD_CN = _rx(r'(下个?|下個?)?\s*(?:周|週|星期|礼拜|禮拜)([一二三四五六日天])')
_WD_EN = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
_RX_WD_EN = _rx(r'\b(next\s+)?(' + '|'.join(_WD_EN) + r')\b', re.I)
_RX_WD_EN_SHORT = _rx(r'\b(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b\.?')
_MONTH_NUM = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
              'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def find_date(text):
    """返回日期描述：('abs', 年|None, 月, 日) / ('rel', 天数) / ('wd', 星期几, 是否下周) / None"""
    m = _RX_YMD.search(text)
    if m:
        return 'abs', int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _RX_CN_MD.search(text)
    if m:
        return 'abs', None, cn2int(m.group(1)), cn2int(m.group(2))
    m = _RX_EN_MD.search(text)
    if m:
        return 'abs', int(m.group(3)) if m.group(3) else None, _MONTH_NUM[m.group(1)[:3].lower()], int(m.group(2))
    m = _RX_EN_DM.search(text)
    if m:
        return 'abs', int(m.group(3)) if m.group(3) else None, _MONTH_NUM[m.group(2)[:3].lower()], int(m.group(1))
    m = _RX_NUM_MD.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            a, b = b, a  # 28/9 这种日/月写法
        y = m.group(3)
        return 'abs', (int(y) + 2000 if y and len(y) == 2 else int(y) if y else None), a, b
    m = _RX_REL.search(text)
    if m:
        return 'rel', dict((w.lower(), n) for w, n in _REL)[m.group(0).lower()]
    m = _RX_WD_CN.search(text)
    if m:
        return 'wd', '一二三四五六日天'.index(m.group(2)) % 7 if m.group(2) != '天' else 6, bool(m.group(1))
    m = _RX_WD_EN.search(text)
    if m:
        return 'wd', _WD_EN.index(m.group(2).lower()), bool(m.group(1))
    m = _RX_WD_EN_SHORT.search(text)
    if m:
        return 'wd', [w[:3] for w in _WD_EN].index(m.group(1)[:3].lower()), False
    return None


def resolve_date(spec, today):
    if spec is None:
        return today, False
    if spec[0] == 'abs':
        _, y, mo, d = spec
        try:
            return date(y or today.year, mo, d), True
        except (TypeError, ValueError):
            return today, False
    if spec[0] == 'rel':
        return today + timedelta(days=spec[1]), True
    _, wd, nxt = spec
    if nxt:
        return today - timedelta(days=today.weekday()) + timedelta(days=7 + wd), True
    return today + timedelta(days=(wd - today.weekday()) % 7), True


# ---------------------------------------------------------------- 对外接口
def parse(text, default_key, now=None):
    """识别文字里的时间。返回 [{'raw','key','zone_given','date_given','dt'}...]，最多 4 个。"""
    text = text.strip()
    if not text or len(text) > MAX_LEN:
        return []
    times = find_times(text)
    if not times:
        return []
    zones = find_zones(text)
    spec = find_date(text)
    now = now or datetime.now(timezone.utc)
    out = []
    for t in times[:4]:
        key, zone_given = pick_zone(t['start'], t['end'], zones, default_key)
        tz = get_tz(key)
        day, date_given = resolve_date(spec, now.astimezone(tz).date())
        dt = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(hours=t['h'], minutes=t['m'])
        out.append({'raw': text[t['start']:t['end']].strip(), 'key': key,
                    'zone_given': zone_given, 'date_given': date_given, 'dt': dt})
    return out


def convert(item):
    """把一个识别结果换算成各时区的行：[(key, 显示名, 当地时间, 相对源日期差几天, 是否源时区)]"""
    src, dt = item['key'], item['dt']
    rows = []
    if src not in ZONES:
        rows.append((src, zone_label(src), dt, 0, True))
    for k in ([src] + [k for k in ORDER if k != src]) if src in ZONES else ORDER:
        local = dt.astimezone(get_tz(k))
        rows.append((k, ZONES[k][0], local, (local.date() - dt.date()).days, k == src))
    return rows
