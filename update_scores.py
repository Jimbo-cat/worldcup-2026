#!/usr/bin/env python3
"""
2026世界杯赛果自动更新脚本
每天 12:00 和 24:00 由 cron 触发。
从 Wikipedia/BBC 抓取比分 → 正则替换 index.html → git commit/push
"""

import re
import os
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

WORKDIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(WORKDIR, "index.html")
BJ_TZ = timezone(timedelta(hours=8))

# ========== 数据源 ==========

def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ⚠ {e.__class__.__name__}: {e}")
        return None


def try_wikipedia():
    html = fetch("https://en.wikipedia.org/wiki/2026_FIFA_World_Cup")
    if not html:
        return {}

    scores = {}
    # Match "TeamA X–Y TeamB" patterns in group tables
    # Wikipedia uses en-dash – for scores in tables
    for m in re.finditer(
        r'>([A-Z][a-zA-Z\s]+?)\s*(\d+)[–\-](\d+)\s*([A-Z][a-zA-Z\s]+?)<',
        html
    ):
        t1, s1, s2, t2 = m.group(1).strip(), m.group(2), m.group(3), m.group(4).strip()
        if len(t1) < 30 and len(t2) < 30:
            scores[(t1, t2)] = f"{s1}-{s2}"
    print(f"  Wikipedia: {len(scores)} 场带比分比赛")
    return scores


def try_bbc():
    html = fetch("https://www.bbc.com/sport/football/world-cup/scores-fixtures")
    if not html:
        return {}
    scores = {}
    # BBC 用 data-score 属性
    for m in re.finditer(r'data-score="(\d+)-(\d+)"', html):
        s1, s2 = m.group(1), m.group(2)
        frag = html[max(0, m.start()-300):m.end()+300]
        teams = re.findall(r'data-team-name="([^"]+)"', frag)
        if len(teams) >= 2:
            scores[(teams[0], teams[1])] = f"{s1}-{s2}"
    # 备用: 文本比分
    if not scores:
        for m in re.finditer(r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s+(\d)[–-](\d)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)', html):
            t1, s1, s2, t2 = m.group(1), m.group(2), m.group(3), m.group(4)
            if len(t1) < 25 and len(t2) < 25:
                scores[(t1, t2)] = f"{s1}-{s2}"
    print(f"  BBC Sport: {len(scores)} 场带比分比赛")
    return scores


# ========== 队伍名标准化映射 ==========

# 英文名 → 中文名（反向查找用）
TEAM_ALIASES = {
    "USA": "United States",
    "U.S.": "United States",
    "America": "United States",
    "Holland": "Netherlands",
    "F.Y.R of Macedonia": "North Macedonia",
    "Macedonia": "North Macedonia",
    "South Korea": "South Korea",
    "Republic of Korea": "South Korea",
    "Ivory Coast": "Ivory Coast",
    "Côte d'Ivoire": "Ivory Coast",
    "Cape Verde": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Bosnia": "Bosnia and Herzegovina",
    "DR Congo": "Congo DR",
    "DRC": "Congo DR",
    "Croatia": "Croatia",
    "Scotland": "Scotland",
    "Algeria": "Algeria",
    "Curacao": "Curacao",
    "Curaçao": "Curacao",
}


def normalize(name):
    n = (name or "").strip()
    n = TEAM_ALIASES.get(n, n)
    return n.lower().replace(" ", "").replace("-", "").replace("'", "")


def uk_to_beijing(day, month, time_str):
    """Convert UK time (BST, UTC+1) to Beijing time (UTC+8)"""
    h, m = map(int, time_str.split(':'))
    total = h * 60 + m + 7 * 60  # +7h from UTC+1 to UTC+8
    new_days, rem = divmod(total, 1440)
    new_h, new_min = divmod(rem, 60)
    bj_day = day + new_days
    bj_month = month
    import calendar
    dim = calendar.monthrange(2026, bj_month)[1]
    if bj_day > dim:
        bj_day -= dim
        bj_month += 1
    return {
        'day': bj_day,
        'month': bj_month,
        'time': f"{new_h:02d}:{new_min:02d}"
    }


def determine_status(d, m, t):
    """判断比赛状态: 'l'=进行中, 'f'=已结束"""
    bj = uk_to_beijing(d, m, t)
    kickoff = datetime(2026, bj['month'], bj['day'],
                       int(bj['time'].split(':')[0]),
                       int(bj['time'].split(':')[1]),
                       tzinfo=BJ_TZ)
    now = datetime.now(BJ_TZ)
    minutes_since = (now - kickoff).total_seconds() / 60
    if minutes_since < 0:
        return 'u'  # 还未开始（不应有比分）
    elif minutes_since < 155:  # ~2h35m = 90min + 15min中场 + 30min加时
        return 'l'  # 进行中
    return 'f'  # 已结束


# ========== 核心 ==========

def update():
    print("=" * 50)
    print(f"🕐 {datetime.now(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')} 更新赛果")
    print("=" * 50)

    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # 收集 HTML 中所有队伍的标准化名字
    html_teams = set()
    for m in re.finditer(r"h:'([^']+)',a:'([^']+)'", html):
        html_teams.add(m.group(1))
        html_teams.add(m.group(2))
    team_norm = {t: normalize(t) for t in html_teams}

    # 尝试数据源
    all_scores = {}
    for fn in [try_wikipedia, try_bbc]:
        try:
            all_scores.update(fn())
        except Exception as e:
            print(f"  ⚠ {fn.__name__}: {e}")

    if not all_scores:
        print("  ℹ️ 所有数据源无新赛果")
        return False

    # 构建 (norm1, norm2) → score 映射
    score_map = {}
    for (t1, t2), sc in all_scores.items():
        n1, n2 = normalize(t1), normalize(t2)
        score_map[(n1, n2)] = sc
        score_map[(n2, n1)] = sc

    # 逐场替换 sc:null → sc:'X-Y',st:'l/f'
    updated = 0
    lines = html.split("\n")
    new_lines = []
    pattern = re.compile(
        r"^(.*?d:(\d+),m:(\d+),.*?t:'([^']+)'.*?)sc:null(.*)$"
    )

    for line in lines:
        m = pattern.match(line)
        if not m:
            new_lines.append(line)
            continue
        prefix, day_str, month_str, time_str, suffix = (
            m.group(1), int(m.group(2)), int(m.group(3)),
            m.group(4), m.group(5)
        )
        # Extract hteam/ateam from prefix for matching
        tm = re.search(r"h:'([^']+)',a:'([^']+)'", prefix)
        if not tm:
            new_lines.append(line)
            continue
        hteam, ateam = tm.group(1), tm.group(2)
        nh, na = normalize(hteam), normalize(ateam)
        key = (nh, na)
        if key in score_map:
            if line.strip().startswith("//"):
                new_lines.append(line)
                continue
            sc = score_map[key]
            st = determine_status(day_str, month_str, time_str)
            new_line = f"{prefix}sc:'{sc}',st:'{st}',et:false,ev:[]" + suffix
            new_lines.append(new_line)
            updated += 1
            print(f"  📝 {hteam} vs {ateam}: {sc} ({st=='l' and '🔴进行中' or '✅已结束'})")
        else:
            new_lines.append(line)

    if updated == 0:
        print("  ℹ️ 没有匹配到需要更新的比赛")
        return False

    new_html = "\n".join(new_lines)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"  ✅ 已更新 {updated} 场比赛到 {HTML_FILE}")
    return True


def git_push():
    """git commit & push"""
    try:
        subprocess.run(["git", "add", "-A"], cwd=WORKDIR, check=True,
                      capture_output=True, timeout=30)
        now = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")
        r = subprocess.run(["git", "commit", "-m", f"auto-update: match scores {now}"],
                          cwd=WORKDIR, capture_output=True, timeout=30, text=True)
        if "nothing to commit" in (r.stdout + r.stderr):
            print("  ℹ️ 无变更，无需提交")
            return True
        print(f"  ✅ Commit: {r.stdout[:100]}")
        subprocess.run(["git", "push"], cwd=WORKDIR, check=True, timeout=60)
        print("  ✅ Push 成功")
        return True
    except subprocess.TimeoutExpired:
        print("  ⚠ Git 超时")
        return False
    except subprocess.CalledProcessError as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        print(f"  ⚠ Git 错误: {(err or out)[:200]}")
        return False


if __name__ == "__main__":
    updated = update()
    if updated:
        git_push()
    else:
        print("  无需推送")
