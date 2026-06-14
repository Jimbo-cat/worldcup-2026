#!/usr/bin/env python3
"""
2026世界杯赛果自动更新脚本
每小时由 cron/launchd 触发。
使用 Wikipedia API 解析分组页面 → 正则替换 index.html → git commit/push
"""

import re
import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from html import unescape

WORKDIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(WORKDIR, "index.html")
BJ_TZ = timezone(timedelta(hours=8))

# Wikipedia group section indices (0-indexed from parse API)
GROUP_SECTIONS = {
    'A': 20, 'B': 21, 'C': 22, 'D': 23,
    'E': 24, 'F': 25, 'G': 26, 'H': 27, 'I': 28, 'J': 29,
    'K': 30, 'L': 31
}

# Proxy config (Clash Verge on 7897)
PROXY = "http://127.0.0.1:7897"
PROXY_HANDLER = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
OPENER = urllib.request.build_opener(PROXY_HANDLER)

# ========== Network ==========

def fetch_json(url, timeout=20):
    """Fetch JSON from URL with proxy."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  ⚠ {e.__class__.__name__}: {e}")
        return None


# ========== Wikipedia API ==========

def fetch_group_section(grp_letter):
    """Fetch a group section's parsed HTML from Wikipedia API."""
    idx = GROUP_SECTIONS.get(grp_letter)
    if not idx:
        return None
    url = (f"https://en.wikipedia.org/w/api.php?"
           f"action=parse&page=2026_FIFA_World_Cup&prop=text&section={idx}&format=json")
    data = fetch_json(url)
    if data and 'parse' in data and 'text' in data['parse']:
        return data['parse']['text']['*']
    return None


def extract_scores_from_section(html):
    """Parse football boxes from a group section HTML.
    Returns dict: normalized_team_key -> {score: str, events: list}
    """
    results = {}

    # Find all football boxes
    boxes = re.findall(
        r'<div[^>]*class="footballbox"[^>]*>.*?</div>\s*</div>\s*</div>',
        html, re.DOTALL
    )

    for box in boxes:
        # Extract home/away team names
        home_m = re.search(
            r'<th class="fhome"[^>]*>.*?<a[^>]*>([^<]+)</a>',
            box, re.DOTALL
        )
        away_m = re.search(
            r'<th class="faway"[^>]*>.*?<a[^>]*>([^<]+)</a>',
            box, re.DOTALL
        )
        if not home_m or not away_m:
            continue

        home = unescape(home_m.group(1).strip())
        away = unescape(away_m.group(1).strip())

        # Extract score: "7–1" format (en-dash) or "Match N" (not yet played)
        score_m = re.search(r'<th class="fscore">.*?(\d+)–(\d+).*?</th>', box, re.DOTALL)
        if not score_m:
            continue  # "Match N" = not played yet

        score = f"{score_m.group(1)}-{score_m.group(2)}"

        # Extract goal events
        events = []
        fgoals = re.search(r'<tr class="fgoals">(.*?)</tr>', box, re.DOTALL)
        if fgoals:
            items = re.findall(r'<li>(.*?)</li>', fgoals.group(1), re.DOTALL)
            for item in items:
                name_m = re.search(r'title="([^"]+)"', item)
                times = re.findall(r'<span[^>]*>(\d+\+?\d*)', item)
                is_pen = 'pen.' in item or 'penalty' in item.lower()
                is_og = 'own goal' in item.lower()
                if name_m and times:
                    ev = {
                        'player': unescape(name_m.group(1)),
                        'minute': times[0],
                        'pen': is_pen,
                        'og': is_og,
                    }
                    events.append(ev)

        key = normalize(f"{home}_{away}")
        results[key] = {'score': score, 'events': events}

        # Also store with away_home key for reverse lookup
        rkey = normalize(f"{away}_{home}")
        results[rkey] = {'score': score, 'events': events, 'reversed': True}

    return results


# ========== Team name handling ==========

TEAM_ALIASES = {
    "USA": "United States",
    "U.S.": "United States",
    "America": "United States",
    "Holland": "Netherlands",
    "South Korea": "South Korea",
    "Republic of Korea": "South Korea",
    "Korea Republic": "South Korea",
    "Korea": "South Korea",
    "Ivory Coast": "Ivory Coast",
    "Côte d'Ivoire": "Ivory Coast",
    "Cape Verde": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Bosnia": "Bosnia and Herzegovina",
    "Curaçao": "Curacao",
    "Curacao": "Curacao",
    "Scotland": "Scotland",
    "DR Congo": "Congo DR",
    "DRC": "Congo DR",
}


def normalize(name):
    n = TEAM_ALIASES.get(name, name)
    return re.sub(r'[^a-z0-9]', '', n.lower())


def match_team_name(html_name, wiki_name):
    """Check if an HTML team name matches a Wikipedia team name."""
    n1 = normalize(html_name)
    n2 = normalize(wiki_name)
    return n1 == n2 or (len(n1) >= 4 and n1 == n2)


# ========== Time utils ==========

def uk_to_beijing(day, month, time_str):
    """Convert UK time (BST, UTC+1) to Beijing time (UTC+8)"""
    h, m = map(int, time_str.split(':'))
    total = h * 60 + m + 7 * 60
    new_days, rem = divmod(total, 1440)
    new_h, new_min = divmod(rem, 60)
    bj_day = day + new_days
    bj_month = month
    import calendar
    dim = calendar.monthrange(2026, bj_month)[1]
    if bj_day > dim:
        bj_day -= dim
        bj_month += 1
    return bj_day, bj_month, f"{new_h:02d}:{new_min:02d}"


def determine_status(d, m, t):
    """判断比赛状态: 'l'=进行中, 'f'=已结束, 'u'=未开始"""
    bj_day, bj_month, bj_time = uk_to_beijing(d, m, t)
    h, mn = map(int, bj_time.split(':'))
    kickoff = datetime(2026, bj_month, bj_day, h, mn, tzinfo=BJ_TZ)
    now = datetime.now(BJ_TZ)
    minutes_since = (now - kickoff).total_seconds() / 60
    if minutes_since < -30:
        return 'u'
    elif minutes_since < 155:
        return 'l'
    return 'f'


def get_all_teams_from_html(lines):
    """Extract all (line_idx, home, away, day, month, time) from MATCHES."""
    teams = []
    for i, line in enumerate(lines):
        m = re.search(r"h:'([^']+)',a:'([^']+)',t:'([^']+)',p:'GS'", line)
        if not m:
            continue
        dm = re.search(r"d:(\d+),m:(\d+)", line)
        if not dm:
            continue
        home = m.group(1)
        away = m.group(2)
        time_str = m.group(3)
        day = int(dm.group(1))
        month = int(dm.group(2))
        teams.append((i, home, away, day, month, time_str))
    return teams


def update():
    print("=" * 50)
    now = datetime.now(BJ_TZ)
    print(f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')} 更新赛果 (北京时间)")
    print("=" * 50)

    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html_lines = f.read().split("\n")

    # Get all group stage matches from HTML
    all_matches = get_all_teams_from_html(html_lines)

    # Find which matches should be finished or in progress
    need_score = []
    for idx, home, away, day, month, time_str in all_matches:
        status = determine_status(day, month, time_str)
        if status == 'f' or status == 'l':
            # Check if score is already filled
            line = html_lines[idx]
            if 'sc:null' in line:
                need_score.append((idx, home, away, day, month, time_str))

    if not need_score:
        print("  ℹ️ 没有需要更新的比赛（所有已完赛比赛均已录入）")
    else:
        print(f"  🔍 {len(need_score)} 场比赛需要更新比分:")
        for _, home, away, day, month, t in need_score:
            bj_day, bj_mon, bj_time = uk_to_beijing(day, month, t)
            print(f"     {home} vs {away} ({bj_mon}/{bj_day} {bj_time})")

    # Try Wikipedia for ALL matches (including ones already scored, to verify)
    print("\n📡 正在从 Wikipedia 获取赛果...")
    all_wiki_scores = {}

    import time
    groups_used = set()
    for idx, home, away, day, month, t_str in all_matches:
        # Find which group this match belongs to
        line = html_lines[idx]
        grp_m = re.search(r"grp:'([A-Z])'", line)
        if not grp_m:
            continue
        grp = grp_m.group(1)
        if grp in groups_used:
            continue
        groups_used.add(grp)
        print(f"   正在解析 {grp} 组...", end=' ')
        html = fetch_group_section(grp)
        if html:
            scores = extract_scores_from_section(html)
            all_wiki_scores.update(scores)
            print(f"获取到 {len(scores)} 场比赛数据")
        else:
            print("⚠ 获取失败")
        time.sleep(1)  # 避免 API 限流

    if not all_wiki_scores:
        print("  ⚠ Wikipedia 未返回任何比赛数据")
        return False

    # Match Wikipedia scores to HTML matches
    updated = 0
    for idx, home, away, day, month, t_str in need_score:
        line = html_lines[idx]
        line_stripped = line.strip()
        if line_stripped.startswith("//"):
            continue

        # Try direct match
        key = normalize(f"{home}_{away}")
        rkey = normalize(f"{away}_{home}")

        wiki = all_wiki_scores.get(key) or all_wiki_scores.get(rkey)
        if not wiki:
            # Fuzzy match
            for wk, wv in all_wiki_scores.items():
                # Check if both teams are present in the key
                h_norm = normalize(home)
                a_norm = normalize(away)
                if h_norm in wk and a_norm in wk and len(wk) <= max(len(h_norm), len(a_norm)) * 2 + 2:
                    wiki = wv
                    break

        if not wiki:
            print(f"  ⚠ 找不到 {home} vs {away} 的比分")
            continue

        score = wiki['score']
        status = 'f'

        # Check if events data is already filled
        has_events = bool(re.search(r"ev:\[.*?\]", line)) and not re.search(r"ev:\[\]", line)

        # Build the replacement string
        # We preserve existing events if they're already set and non-empty
        new_line = re.sub(
            r"sc:null",
            f"sc:'{score}'",
            line, count=1
        )
        new_line = re.sub(
            r"(sc:'[^']+')\s*,\s*(?!(?:st|et|ev))",  # don't drop st/et/ev
            r"\1,",
            new_line, count=1
        )

        # Add st and et fields if missing
        if "st:'" not in new_line:
            new_line = re.sub(r"sc:'[^']+'", f"sc:'{score}',st:'{status}'", new_line, count=1)
        else:
            new_line = re.sub(r"st:'[^lfu]'", f"st:'{status}'", new_line)

        # Add empty events if line has none
        if "ev:" not in new_line:
            new_line = re.sub(r"et:false", "et:false,ev:[]", new_line)

        html_lines[idx] = new_line
        updated += 1
        print(f"  ✅ {home} {score} {away}")

    if updated == 0:
        print("\n  ℹ️ 没有匹配到需要更新的比赛")
        return False

    new_html = "\n".join(html_lines)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"\n  ✅ 已更新 {updated} 场比赛到 index.html")
    return True


def git_push():
    try:
        subprocess.run(["git", "add", "-A"], cwd=WORKDIR, check=True,
                      capture_output=True, timeout=30)
        now = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")
        r = subprocess.run(["git", "commit", "-m", f"auto-update: match scores {now}"],
                          cwd=WORKDIR, capture_output=True, timeout=30, text=True)
        if "nothing to commit" in (r.stdout + r.stderr):
            print("  ℹ️ 无变更，无需提交")
            return True
        print(f"  ✅ Commit: {r.stdout[:100] if r.stdout else 'ok'}")
        subprocess.run(["git", "push"], cwd=WORKDIR, check=True, timeout=60)
        print("  ✅ Push 成功")
        return True
    except subprocess.TimeoutExpired:
        print("  ⚠ Git 超时")
        return False
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        out = (e.stdout or "").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        print(f"  ⚠ Git 错误: {(err or out)[:200]}")
        return False
    except Exception as e:
        print(f"  ⚠ Git 异常: {e}")
        return False


if __name__ == "__main__":
    updated = update()
    if updated:
        git_push()
    else:
        print("  无需推送")
