#!/usr/bin/env python3
"""
Web3 速递推送（GitHub Actions 云端版 v2）
- 每天北京时间 6 / 9 / 12 / 15 / 18 点共五档推送
- 每档只推上一档之后的新消息（状态存 state.json，由 workflow 回写仓库）
- 内容配比：X/推特风向（中文加密媒体实时搬运 X 上 KOL 动态）约一半
  + 全网动态约一半；另设 空投速递 / 新项目雷达 专项板块
- 英文标题自动翻译中文，失败回退原文
仅用 Python 标准库，无第三方依赖。
"""
import os
import re
import sys
import time
import html
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------- 基础配置 ----------------
CST = timezone(timedelta(hours=8))
SLOT_HOURS = [6, 9, 12, 15, 18]          # 北京时间五档
FALLBACK_WINDOW = 3.5                     # 无状态文件时回看的小时数
OVERLAP_MIN = 10                          # 与上一档的重叠分钟数，防漏
MAX_PER_SOURCE = 8                        # 单源上限
SEEN_KEEP = 300                           # 记住最近多少条已推过的标题

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SENDKEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sendkey")
if not SENDKEY and os.path.exists(SENDKEY_FILE):
    SENDKEY = open(SENDKEY_FILE, encoding="utf-8").read().strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

DRY_RUN = "--dry-run" in sys.argv
FORCE = os.environ.get("PUSH_FORCE") == "1" or "--force" in sys.argv

UA = {"User-Agent": "Mozilla/5.0 (compatible; web3-daily/2.0)"}
TRANSLATE = "--no-translate" not in sys.argv
_CJK = re.compile(r"[\u4e00-\u9fff]")

# ---------------- 数据源 ----------------
GN_ZH = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GN_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def gn(q, zh=True):
    return (GN_ZH if zh else GN_EN).format(q=urllib.parse.quote(q))


FEEDS = [
    # X / 推特风向：中文加密媒体实时搬运 X 上的 KOL 发文、项目方公告
    # 直连源（若云端网络不通会自动降级到 Google News 聚合源）
    dict(group="x", name="X·BlockBeats快讯", url="https://rss.theblockbeats.info/flash"),
    dict(group="x", name="X·PANews快讯", url="https://rss.panewslab.com/zh/whatsnew"),
    dict(group="x", name="X·中文快讯",
         url=gn("site:theblockbeats.info OR site:panewslab.com OR site:odaily.news "
                "OR site:jinse.cn OR site:foresightnews.pro")),
    dict(group="x", name="X·KOL热议",
         url=gn("推特 KOL 加密货币 OR web3 OR 代币 OR 空投")),
    # 全网动态
    dict(group="web", name="CoinDesk",
         url="https://www.coindesk.com/arc/outboundfeeds/rss/"),
    dict(group="web", name="Cointelegraph", url="https://cointelegraph.com/rss"),
    dict(group="web", name="Decrypt", url="https://decrypt.co/feed"),
    dict(group="web", name="英文聚合", url=gn("web3 OR crypto project launch OR funding", zh=False)),
    dict(group="web", name="中文聚合", url=gn("web3 加密项目")),
    # 空投速递
    dict(group="airdrop", name="空投·中文", url=gn("加密货币 空投 airdrop")),
    dict(group="airdrop", name="空投·英文", url=gn("crypto airdrop", zh=False)),
    # 新项目雷达
    dict(group="newproj", name="新项目·融资主网", url=gn("加密 新项目 融资 OR 主网 OR 上线")),
    dict(group="newproj", name="新项目·TGE",
         url=gn("token generation event OR TGE OR mainnet launch", zh=False)),
]

GROUPS = [
    ("x", "🐦 X / 推特风向", 14),
    ("web", "🌐 全网动态", 14),
    ("airdrop", "🪂 空投速递", 8),
    ("newproj", "🚀 新项目雷达", 8),
]

# 关键词路由：全池新闻按关键词分流到专项板块（空投 > 新项目 > 默认板块）
AIRDROP_KW = ["空投", "airdrop", "测试网", "testnet", "claim", "领取"]
NEWPROJ_KW = ["融资", "主网", "mainnet", "tge", "代币生成", "launchpad",
              "公募", "raise", "series a", "series b", "listing", "上线",
              "token launch", "mint"]


def route(default_group, title):
    t = title.lower()
    if any(k in t for k in AIRDROP_KW):
        return "airdrop"
    if any(k in t for k in NEWPROJ_KW):
        return "newproj"
    return default_group


# ---------------- 状态管理 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        return {"slots_done": s.get("slots_done", []),
                "last_push_at": s.get("last_push_at"),
                "seen": s.get("seen", [])}
    except Exception:
        return {"slots_done": [], "last_push_at": None, "seen": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"slots_done": state["slots_done"][-20:],
                   "last_push_at": state["last_push_at"],
                   "seen": state["seen"][-SEEN_KEEP:]}, f, ensure_ascii=False, indent=1)


def current_slot(now_cst):
    """返回当前时刻所属档位的 datetime（CST）；凌晨 0~6 点归前一晚 18 点档。"""
    for h in reversed(SLOT_HOURS):
        if now_cst.hour > h or (now_cst.hour == h and now_cst.minute >= 0):
            return now_cst.replace(hour=h, minute=0, second=0, microsecond=0)
    return (now_cst - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)


# ---------------- 抓取与解析 ----------------
def translate(text, retries=2):
    if not TRANSLATE or _CJK.search(text):
        return text
    q = urllib.parse.quote(text[:500])
    url = ("https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl=en&tl=zh-CN&dt=t&q={q}")
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
            if out.strip():
                return out.strip()
        except Exception:
            if i < retries:
                time.sleep(1.5)
    return text


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_text(s):
    s = html.unescape(s or "").strip()
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def collect_items(xml_bytes, since_dt):
    """兼容 RSS2.0 / RDF / Atom，返回 [(dt, title, link)]，只保留 since 之后发布的。"""
    items = []
    root = ET.fromstring(xml_bytes)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        title = parse_text(it.findtext("title"))
        link = parse_text(it.findtext("link") or "")
        pub = parse_time(it.findtext("pubDate") or it.findtext(
            "{http://purl.org/dc/elements/1.1/}date"))
        if title:
            items.append((pub, title, link))
    for it in root.findall("atom:entry", ns):
        title = parse_text(it.findtext("atom:title", namespaces=ns))
        link_el = it.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        pub = parse_time(it.findtext("atom:updated", namespaces=ns) or
                         it.findtext("atom:published", namespaces=ns))
        if title:
            items.append((pub, title, link))
    out = []
    for pub, title, link in items:
        if pub is None:
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if since_dt <= pub <= datetime.now(timezone.utc) + timedelta(hours=1):
            out.append((pub, title, link))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:MAX_PER_SOURCE]


def key_of(title):
    return re.sub(r"\s+", "", title)[:60].lower()


# ---------------- 简报生成 ----------------
def build_briefing(since_dt, seen_before):
    """返回 (title, desp, new_keys, failed_sources)"""
    buckets = {g: [] for g, _, _ in GROUPS}
    seen = set(seen_before)
    failed = []
    for feed in FEEDS:
        try:
            items = collect_items(fetch(feed["url"]), since_dt)
        except Exception as e:
            failed.append(f'{feed["name"]}({type(e).__name__})')
            continue
        for pub, title, link in items:
            k = key_of(title)
            if k in seen:
                continue
            seen.add(k)
            buckets[route(feed["group"], title)].append((pub, title, link))

    sections, total = [], 0
    for g, header, cap in GROUPS:
        items = buckets[g][:cap]
        if not items:
            continue
        lines = [f"### {header}", ""]
        for pub, title, link in items:
            zh = translate(title)
            show = zh if len(zh) <= 80 else zh[:80] + "…"
            tag = pub.astimezone(CST).strftime("%H:%M")
            if link:
                lines.append(f"- [{show}]({link}) `{tag}`")
            else:
                lines.append(f"- {show} `{tag}`")
            total += 1
            time.sleep(0.2)
        lines.append("")
        sections.append("\n".join(lines))

    now_cst = datetime.now(CST)
    slot_dt = current_slot(now_cst)
    title = f"Web3速递 {now_cst:%m-%d} {slot_dt:%H}点档"
    if total == 0:
        return title, "", [], failed
    desp = (f"⏱️ 本档新增 **{total}** 条（覆盖自上一档以来的新消息）\n\n"
            + "\n".join(sections))
    if failed:
        desp += f"\n*部分源异常：{('、'.join(failed))[:60]}*\n"
    desp += "\n---\n*GitHub Actions 自动推送 · 非投资建议*"
    new_keys = [key_of(t) for g, _, _ in GROUPS for _, t, _ in buckets[g][:cap]]
    return title, desp, new_keys, failed


# ---------------- 推送 ----------------
def push(title, desp):
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(
        f"https://sctapi.ftqq.com/{SENDKEY}.send", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


# ---------------- 主流程 ----------------
def main():
    now_cst = datetime.now(CST)
    slot_dt = current_slot(now_cst)
    slot_key = slot_dt.strftime("%Y-%m-%d_%H")
    state = load_state()

    if not FORCE and slot_key in state["slots_done"]:
        print(f"{slot_key} 档已推送过，本班次跳过（防重发）")
        return

    # 计算增量窗口
    if state["last_push_at"]:
        try:
            last = datetime.fromisoformat(state["last_push_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            since_dt = last.astimezone(timezone.utc) - timedelta(minutes=OVERLAP_MIN)
        except Exception:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=FALLBACK_WINDOW)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=FALLBACK_WINDOW)
    print(f"档位: {slot_key} | 增量窗口起点(UTC): {since_dt:%Y-%m-%d %H:%M}")

    title, desp, new_keys, failed = build_briefing(since_dt, state["seen"])

    if not desp:
        # 无新增：标记档位完成但不动 last_push_at（下一档自动补上这段窗口）
        state["slots_done"].append(slot_key)
        if not DRY_RUN:
            save_state(state)
        print("本档无新增消息，不推送（不消耗额度）")
        return

    print(f"== {title} == 新增 {len(new_keys)} 条")
    print(desp[:600])
    if DRY_RUN:
        print("\n[DRY RUN] 未实际推送、未写状态")
        return

    if not SENDKEY:
        print("ERROR: 未配置 SENDKEY")
        sys.exit(1)

    resp = push(title, desp)
    print("PUSH RESULT:", resp)
    if '"code":0' not in resp:
        print("推送失败，不写状态（下一班次自愈重试）")
        sys.exit(2)

    state["slots_done"].append(slot_key)
    state["last_push_at"] = datetime.now(CST).isoformat()
    state["seen"] = (state["seen"] + new_keys)[-SEEN_KEEP:]
    save_state(state)
    print("状态已更新并写回 state.json")


if __name__ == "__main__":
    main()
