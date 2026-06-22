#!/usr/bin/env python3
"""
2026世界杯赛果自动更新脚本
每小时由 cron/launchd 触发。
使用 Wikipedia API 解析分组页面 → 正则替换 index.html → git commit/push
"""

import re
import os
import json
import unicodedata
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from html import unescape

WORKDIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(WORKDIR, "index.html")
BJ_TZ = timezone(timedelta(hours=8))

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

def fetch_full_page():
    """Fetch the entire parsed 2026 World Cup page HTML in one API call."""
    url = ("https://en.wikipedia.org/w/api.php?"
           "action=parse&page=2026_FIFA_World_Cup&prop=text&format=json")
    data = fetch_json(url)
    if data and 'parse' in data and 'text' in data['parse']:
        return data['parse']['text']['*']
    return None


def parse_event_item(item, side):
    """Parse a single <li> event item from a football box goals cell."""
    name_m = re.search(r'title="([^"]+)"', item)
    if not name_m:
        return None
    times = re.findall(r'<span[^>]*>(\d+\+?\d*)', item)
    if not times:
        return None

    player = unescape(name_m.group(1))
    minute = times[0]
    penalty = bool(re.search(r'class="penalty"', item))
    own_goal = bool(re.search(r'class="own-goal"', item))

    return {
        'player': player,
        'minute': minute,
        'side': side,
        'penalty': penalty,
        'own_goal': own_goal,
    }


def generate_html_events(events):
    """Convert Wikipedia events to rich Chinese JS object literal format.

    Returns a string like:
    [{min:10,t:'h',p:'Player',ic:'⚽',d:'...'},{...}]
    Returns '[]' for empty/no events.

    Produces varied Chinese descriptions with:
    - Different scoring verbs
    - Brace/hat-trick detection
    - Proper handling of penalties, own goals
    - Scoreline context
    """
    if not events:
        return "[]"

    # Sort by minute
    sorted_ev = sorted(
        events,
        key=lambda e: (
            int(e['minute'].split('+')[0])
            if '+' in e['minute']
            else int(e['minute'])
        ),
    )

    # Track goals per player for brace/hat-trick detection
    player_goals_h = {}
    player_goals_a = {}
    home_goals = 0
    away_goals = 0
    parts = []

    # Verb templates for variety (rotate based on goal index)
    GOAL_VERBS = [
        "劲射破门",
        "捅射入网",
        "抽射得分",
        "推射建功",
        "攻入一球",
        "抢点破门",
        "低射得手",
        "包抄到位",
    ]

    for ev in sorted_ev:
        if ev['side'] == 'h':
            cur_side_goals = home_goals
            home_goals += 1
            pg = player_goals_h
        else:
            cur_side_goals = away_goals
            away_goals += 1
            pg = player_goals_a

        current_score = f"{home_goals}-{away_goals}"

        # How many times has THIS player scored so far (including this one)?
        player = ev['player']
        gc = pg.get(player, 0) + 1
        pg[player] = gc

        # Icon
        if ev['own_goal']:
            ic = '⚽(og)'
        elif ev['penalty']:
            ic = '⚽(P)'
        else:
            ic = '⚽'

        # Chinese description — richness depends on goal type & count
        if ev['own_goal']:
            desc = f"{player} 不慎自摆乌龙，比分 {current_score}"
        elif ev['penalty']:
            desc = f"{player} 点球一蹴而就！比分 {current_score}"
        else:
            # Use a verb from the template for variety
            verb_idx = (cur_side_goals + len(parts)) % len(GOAL_VERBS)
            verb = GOAL_VERBS[verb_idx]
            if gc == 2:
                desc = f"{player} 梅开二度！{verb}，比分 {current_score}"
            elif gc == 3:
                desc = f"{player} 帽子戏法！{verb}，比分 {current_score}"
            elif gc > 3:
                desc = f"{player} 大四喜！{verb}，比分 {current_score}"
            else:
                desc = f"{player} {verb}，比分 {current_score}"

        p_esc = player.replace("'", "\\'")
        d_esc = desc.replace("'", "\\'")

        parts.append(
            f"{{min:{ev['minute']},t:'{ev['side']}',"
            f"p:'{p_esc}',ic:'{ic}',d:'{d_esc}'}}"
        )

    return "[" + ",".join(parts) + "]"


def extract_all_scores(html):
    """Parse ALL football boxes from the full page HTML.
    Returns dict of norm_teamkey -> {'score': str, 'events': list}
    Now extracts richer event data (side, penalty, own_goal).
    """
    results = {}

    # Find all football boxes across the entire page
    boxes = re.findall(
        r'<div[^>]*class="footballbox"[^>]*>.*?</div>\s*</div>\s*</div>',
        html, re.DOTALL
    )

    for box in boxes:
        home_m = re.search(
            r'<th class="fhome"[^>]*>.*?<a[^>]*>([^<]+)</a>', box, re.DOTALL
        )
        away_m = re.search(
            r'<th class="faway"[^>]*>.*?<a[^>]*>([^<]+)</a>', box, re.DOTALL
        )
        if not home_m or not away_m:
            continue

        home = unescape(home_m.group(1).strip())
        away = unescape(away_m.group(1).strip())

        # Score
        score_m = re.search(
            r'<th class="fscore">.*?(\d+)–(\d+).*?</th>', box, re.DOTALL
        )
        if not score_m:
            continue

        score = f"{score_m.group(1)}-{score_m.group(2)}"

        # Goals with side detection
        events = []
        fgoals = re.search(r'<tr class="fgoals">(.*?)</tr>', box, re.DOTALL)
        if fgoals:
            home_goals = re.search(
                r'<td[^>]*class="fhgoal"[^>]*>(.*?)</td>',
                fgoals.group(1), re.DOTALL,
            )
            if home_goals:
                for item in re.findall(
                    r'<li>(.*?)</li>', home_goals.group(1), re.DOTALL
                ):
                    ev = parse_event_item(item, 'h')
                    if ev:
                        events.append(ev)

            away_goals = re.search(
                r'<td[^>]*class="fagoal"[^>]*>(.*?)</td>',
                fgoals.group(1), re.DOTALL,
            )
            if away_goals:
                for item in re.findall(
                    r'<li>(.*?)</li>', away_goals.group(1), re.DOTALL
                ):
                    ev = parse_event_item(item, 'a')
                    if ev:
                        events.append(ev)

        h_norm = normalize(home)
        a_norm = normalize(away)
        key = f"{h_norm}_{a_norm}"
        rkey = f"{a_norm}_{h_norm}"
        results[key] = {'score': score, 'events': events}
        results[rkey] = {'score': score, 'events': events, 'reversed': True}

    return results


# ========== Team name handling ==========

TEAM_ALIASES = {
    "USA": "United States",
    "U.S.": "United States",
    "America": "United States",
    "Holland": "Netherlands",
    "Republic of Korea": "South Korea",
    "Korea Republic": "South Korea",
    "Korea": "South Korea",
    "Côte d'Ivoire": "Ivory Coast",
    "Cabo Verde": "Cape Verde",
    "Bosnia": "Bosnia and Herzegovina",
    "Curaçao": "Curacao",
    "DR Congo": "Congo DR",
    "DRC": "Congo DR",
}


def normalize(name):
    """Normalize a team name for comparison (iterative, no recursion).

    Uses Unicode NFKD decomposition to strip diacritics (curaçao → curacao)
    before stripping non-ASCII chars, so aliases with accented characters work.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    seen = set()
    while True:
        matched = False
        for alias, canonical in TEAM_ALIASES.items():
            a = alias.strip().lower()
            a = unicodedata.normalize("NFKD", a)
            a = re.sub(r"[^a-z0-9]", "", a)
            if n == a:
                c = canonical.strip().lower()
                c = unicodedata.normalize("NFKD", c)
                c = re.sub(r"[^a-z0-9]", "", c)
                if c in seen:
                    return n  # cycle guard
                seen.add(c)
                n = c
                matched = True
                break
        if not matched:
            return n


# ========== HTML Helpers ==========

def get_all_teams_from_html(lines):
    """Find all MATCHES entries that still have sc:null (unplayed) and
    return list of (line_index, home_team, away_team, day, month, time_str)."""
    matches = []
    for idx, line in enumerate(lines):
        line = line.strip()
        if line.startswith("//") or not line:
            continue
        # Match a JS object line
        m = re.match(
            r'\{d:(\d+),m:(\d+),grp:\'[^\']+\',h:\'([^\']+)\',a:\'([^\']+)\''
            r',t:\'([^\']+)\'.*\}',
            line
        )
        if m:
            day = int(m.group(1))
            month = int(m.group(2))
            home = m.group(3)
            away = m.group(4)
            time_str = m.group(5)
            matches.append((idx, home, away, day, month, time_str))
    return matches


# ========== Time handling ==========

def uk_to_beijing(day, month, time_str):
    """Convert UK time to Beijing time.
    UK is currently BST (UTC+1). Beijing is UTC+8.
    """
    def parse_minutes(t):
        parts = t.split(':')
        return int(parts[0]) * 60 + int(parts[1])

    def format_time(minutes):
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # UK is UTC+1 (BST), Beijing is UTC+8 -> +7 hours
    uk_minutes = parse_minutes(time_str)
    bj_minutes = uk_minutes + 7 * 60

    # Handle day rollover
    bj_day = day + (bj_minutes // (24 * 60))
    bj_minutes = bj_minutes % (24 * 60)
    bj_mon = month

    # Simple month rollover check (June has 30 days)
    if bj_mon == 6 and bj_day > 30:
        bj_day -= 30
        bj_mon = 7
    elif bj_mon == 7 and bj_day > 31:
        bj_day -= 31
        bj_mon = 8

    return bj_day, bj_mon, format_time(bj_minutes)


def determine_status(day, month, time_str):
    """Check if a match should be finished, live, or upcoming.
    Returns 'f', 'l', or 'u'.
    Returns 'u' for TBC/unknown times.
    """
    if time_str == 'TBC' or not time_str:
        return 'u'

    try:
        parts = time_str.split(':')
        start_minutes = int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 'u'

    now_utc = datetime.now(timezone.utc)
    # UK is BST = UTC+1
    uk_now = now_utc.astimezone(timezone(timedelta(hours=1)))

    # Handle day rollover for end time (match starts late, ends next day)
    end_minutes = start_minutes + 120
    end_day_offset = end_minutes // (24 * 60)
    end_minutes = end_minutes % (24 * 60)

    try:
        match_start = uk_now.replace(
            hour=start_minutes // 60,
            minute=start_minutes % 60,
            second=0, microsecond=0,
        ).replace(day=day, month=month)

        match_end = uk_now.replace(
            hour=end_minutes // 60,
            minute=end_minutes % 60,
            second=0, microsecond=0,
        ).replace(day=day + end_day_offset, month=month)
    except (ValueError, OverflowError):
        return 'u'

    if match_end < uk_now:
        return 'f'
    elif match_start <= uk_now <= match_end:
        return 'l'
    else:
        return 'u'


# ========== Main update logic ==========

def update():
    """Fetch scores from Wikipedia and update index.html."""
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_lines = html_content.split("\n")

    # Get all group stage matches from HTML
    all_matches = get_all_teams_from_html(html_lines)

    # Find which matches should be finished or in progress
    need_score = []
    need_events = []
    for idx, home, away, day, month, time_str in all_matches:
        status = determine_status(day, month, time_str)
        if status == 'f' or status == 'l':
            line = html_lines[idx]
            # Need score if null
            if 'sc:null' in line:
                need_score.append((idx, home, away, day, month, time_str))
            # Need events if empty events array AND has a score
            elif re.search(r'ev:\s*\[\s*\]', line) and re.search(r"sc:'[^']+'", line):
                need_events.append((idx, home, away, day, month, time_str))

    if not need_score:
        print("  ℹ️ 没有需要更新比分的比赛")
    else:
        print(f"  🔍 {len(need_score)} 场比赛需要更新比分:")
        for _, home, away, day, month, t in need_score:
            bj_day, bj_mon, bj_time = uk_to_beijing(day, month, t)
            print(f"     {home} vs {away} ({bj_mon}/{bj_day} {bj_time})")

    if need_events:
        print(f"  🔍 {len(need_events)} 场比赛需要补填进球事件:")
        for _, home, away, day, month, t in need_events:
            print(f"     {home} vs {away}")

    # Try Wikipedia for ALL matches
    print("\n📡 正在从 Wikipedia 获取赛果...")
    all_wiki_scores = {}

    # Single request for the whole page
    print("  正在获取完整页面...", end=' ')
    full_html = fetch_full_page()
    if full_html:
        print(f"成功 ({len(full_html)} chars)")
        all_wiki_scores = extract_all_scores(full_html)
        print(f"  Wikipedia: 解析到 {len(all_wiki_scores)} 场比赛数据")
    else:
        print("⚠ 获取失败")

    if not all_wiki_scores:
        print("  ⚠ Wikipedia 未返回任何比赛数据")
        return False

    # Match Wikipedia scores to HTML matches
    updated_score = 0
    updated_events = 0

    # Helper: look up a match in wiki data
    def lookup_wiki(home, away, wiki_data):
        h_norm = normalize(home)
        a_norm = normalize(away)
        key = f"{h_norm}_{a_norm}"
        rkey = f"{a_norm}_{h_norm}"
        wiki = wiki_data.get(key) or wiki_data.get(rkey)
        if not wiki:
            for wk, wv in wiki_data.items():
                if h_norm in wk and a_norm in wk and len(wk) <= max(len(h_norm), len(a_norm)) * 2 + 2:
                    wiki = wv
                    break
        return wiki

    # Process matches needing score updates
    for idx, home, away, day, month, t_str in need_score:
        line = html_lines[idx]
        line_stripped = line.strip()
        if line_stripped.startswith("//"):
            continue

        wiki = lookup_wiki(home, away, all_wiki_scores)
        if not wiki:
            print(f"  ⚠ 找不到 {home} vs {away} 的比分")
            continue

        score = wiki['score']
        status = 'f'

        # Build the replacement string
        new_line = re.sub(
            r"sc:null",
            f"sc:'{score}'",
            line, count=1
        )

        # Add st field if missing
        if "st:'" not in new_line:
            new_line = re.sub(r"sc:'[^']+'", f"sc:'{score}',st:'{status}'", new_line, count=1)
        else:
            new_line = re.sub(r"st:'[^lfu]'", f"st:'{status}'", new_line)

        # Add events
        ev_str = generate_html_events(wiki.get('events', []))
        if "ev:" not in new_line:
            new_line = re.sub(r"et:false", f"et:false,ev:{ev_str}", new_line)
        else:
            new_line = re.sub(r"ev:\s*\[.*?\]", f"ev:{ev_str}", new_line)

        html_lines[idx] = new_line
        updated_score += 1
        print(f"  ✅ {home} {score} {away} ({len(wiki.get('events',[]))} events)")

    # Process matches needing event fills (already have score, empty events)
    for idx, home, away, day, month, t_str in need_events:
        line = html_lines[idx]
        line_stripped = line.strip()
        if line_stripped.startswith("//"):
            continue

        wiki = lookup_wiki(home, away, all_wiki_scores)
        if not wiki:
            continue

        if not wiki.get('events'):
            print(f"  ℹ️ {home} vs {away}: Wikipedia 暂无进球事件数据")
            continue

        ev_str = generate_html_events(wiki['events'])
        new_line = re.sub(r"ev:\s*\[\s*\]", f"ev:{ev_str}", line)
        html_lines[idx] = new_line
        updated_events += 1
        print(f"  ✅ {home} vs {away}: 补填 {len(wiki['events'])} 个进球事件")

    if updated_score == 0 and updated_events == 0:
        print("\n  ℹ️ 没有匹配到需要更新的比赛")
        return False

    new_html = "\n".join(html_lines)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"\n  ✅ 已更新 {updated_score} 场比分 + {updated_events} 场进球事件到 index.html")
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
