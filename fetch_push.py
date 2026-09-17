#!/usr/bin/env python3
"""
每日 Web3 简报推送（GitHub Actions 云端版）
- 拉取多个 RSS 源的最近 24 小时新闻
- 汇总为 Markdown 简报
- 通过 Server酱推送到微信
仅用 Python 标准库，无需安装任何依赖。
"""
import os
import re
import sys
import html
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------- 配置 ----------------
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SENDKEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sendkey")
if not SENDKEY and os.path.exists(SENDKEY_FILE):
    SENDKEY = open(SENDKEY_FILE, encoding="utf-8").read().strip()

FEEDS = [
    # 英文主源
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    # Google News 聚合：英文 web3/crypto 项目动态
    ("Web3-EN", "https://news.google.com/rss/search?q=web3+OR+crypto+project+launch+OR+funding&hl=en-US&gl=US&ceid=US:en"),
    # Google News 聚合：中文加密动态
    ("Web3-中文", "https://news.google.com/rss/search?q=web3+%E5%8A%A0%E5%AF%86%E9%A1%B9%E7%9B%AE&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
]

MAX_PER_SOURCE = 6          # 每个源最多取几条
WINDOW_HOURS = 24           # 只取最近 24 小时
DRY_RUN = "--dry-run" in sys.argv

UA = {"User-Agent": "Mozilla/5.0 (compatible; web3-daily/1.0)"}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_text(s):
    s = html.unescape(s or "").strip()
    s = re.sub(r"<[^>]+>", "", s)          # 去残余标签
    s = re.sub(r"\s+", " ", s)
    return s


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)     # RFC822 (RSS)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO (Atom)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def collect_items(xml_bytes, source):
    """兼容 RSS2.0 / RDF / Atom，返回 [(dt, title, link)]"""
    items = []
    root = ET.fromstring(xml_bytes)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    # RSS / RDF
    for it in root.iter("item"):
        title = parse_text(it.findtext("title"))
        link = parse_text(it.findtext("link") or "")
        pub = parse_time(it.findtext("pubDate") or it.findtext(
            "{http://purl.org/dc/elements/1.1/}date"))
        if title:
            items.append((pub, title, link))
    # Atom
    for it in root.findall("atom:entry", ns):
        title = parse_text(it.findtext("atom:title", namespaces=ns))
        link_el = it.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        pub = parse_time(it.findtext("atom:updated", namespaces=ns) or
                         it.findtext("atom:published", namespaces=ns))
        if title:
            items.append((pub, title, link))
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(hours=WINDOW_HOURS)
    out = []
    for pub, title, link in items:
        if pub is None or pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc) if pub else now
        if pub >= deadline:
            out.append((pub, title, link))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:MAX_PER_SOURCE]


def build_briefing():
    now = datetime.now(timezone.utc) + timedelta(hours=8)  # 北京时间
    sections, seen = [], set()
    total = 0
    for name, url in FEEDS:
        try:
            items = collect_items(fetch(url), name)
        except Exception as e:
            sections.append(f"**{name}**：拉取失败（{type(e).__name__}）\n")
            continue
        if not items:
            continue
        lines = [f"### {name}", ""]
        for pub, title, link in items:
            key = title[:60].lower()
            if key in seen:
                continue
            seen.add(key)
            tag = pub.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
            show = title if len(title) <= 80 else title[:80] + "…"
            if link:
                lines.append(f"- [{show}]({link}) `{tag}`")
            else:
                lines.append(f"- {show} `{tag}`")
            total += 1
        lines.append("")
        if len(lines) > 3:
            sections.append("\n".join(lines))
    title = f"每日Web3简报 {now:%Y-%m-%d}"
    if total == 0:
        desp = "**今日未抓取到新闻，可能是数据源临时异常，请查看 Actions 运行日志。**"
    else:
        desp = ("🔥 过去24小时 Web3 / 加密项目动态速览 "
                f"（共 {total} 条）\n\n" + "\n".join(sections) +
                "\n---\n*由 GitHub Actions 自动推送，非投资建议*")
    return title, desp


def push(title, desp):
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(
        f"https://sctapi.ftqq.com/{SENDKEY}.send", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def main():
    if not SENDKEY and not DRY_RUN:
        print("ERROR: 未配置 SENDKEY（设置 secret SERVERCHAN_SENDKEY 或本地 .sendkey 文件）")
        sys.exit(1)
    title, desp = build_briefing()
    print(f"== {title} ==")
    print(desp[:800])
    if DRY_RUN:
        print("\n[DRY RUN] 未实际推送")
        return
    resp = push(title, desp)
    print("PUSH RESULT:", resp)
    if '"code":0' not in resp:
        sys.exit(2)


if __name__ == "__main__":
    main()
