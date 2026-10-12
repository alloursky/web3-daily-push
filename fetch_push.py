#!/usr/bin/env python3
"""
全网热点速递（GitHub Actions 云端版 v4）
- 每天北京时间 7 / 8 / 9 / 10 / 11 点五档推送（定时器挂 5-9 点，
  对冲 GitHub 定时延迟约 2 小时），12 点前全部发完
- 每档 5~10 条：全网热搜（微博为主）+ AI 风向 + 币圈风向（保底防跑偏）
- 优先热度上升快的词条；同一条不重复，热度暴涨可"升温速报"重推一次
- 涉政/领导人/重大负面自动过滤；娱乐八卦每档最多 3 条
- 纯文字（无图片）：微信 Markdown 明细 + 可复制发推文案
- 额度保护：Server酱免费版每天 5 条，自己记账，超额不硬发
- 可靠性：多源冗余、每源留日志、源大面积失败不消耗档位
仅用 Python 标准库。
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
SLOT_HOURS = [5, 6, 7, 8, 9]             # 定时器档位（北京），实际到达约 7-11 点
DISPLAY_OFFSET = 2                        # 展示用档位 = 定时档位 + 2
MAX_PER_SOURCE = 6
SEEN_KEEP = 400
TOTAL_CAP = 10
MIN_ITEMS = 5                             # 每档目标 5-10 条
DAILY_QUOTA = 5                           # Server酱免费版每天 5 条
GOSSIP_CAP = 3                            # 娱乐八卦每档上限

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SENDKEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sendkey")
if not SENDKEY and os.path.exists(SENDKEY_FILE):
    SENDKEY = open(SENDKEY_FILE, encoding="utf-8").read().strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

DRY_RUN = "--dry-run" in sys.argv
FORCE = os.environ.get("PUSH_FORCE") == "1" or "--force" in sys.argv

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TRANSLATE = "--no-translate" not in sys.argv
_CJK = re.compile(r"[一-鿿]")

# ---------------- 内容过滤 ----------------
# 涉政/领导人/重大负面：一律不推（账号安全红线）
SENSITIVE_KW = [
    "习近平", "李强", "李克强", "王沪宁", "赵乐际", "蔡奇", "丁薛祥", "李希",
    "王毅", "政治局", "中南海", "国务院", "党中央", "全国人大", "全国政协",
    "人大代表", "政协委员", "市委书记", "省委书记", "连任", "就职演说",
    "台湾当局", "台独", "港独", "藏独", "疆独", "法轮",
    "地震", "遇难", "身亡", "坠机", "空难", "爆炸案", "枪击", "恐袭",
    "自杀", "他杀", "命案", "灭门", "强奸", "猥亵儿童", "死刑执行",
]
# 娱乐八卦识别（每档限 3 条）
GOSSIP_KW = [
    "恋情", "官宣", "分手", "离婚", "结婚", "婚礼", "出轨", "绯闻", "塌房",
    "演唱会", "综艺", "路透", "男团", "女团", "爱豆", "顶流", "小花", "小生",
    "电视剧", "新剧", "开机", "杀青", "票房", "首映", "导演", "主演",
]


def is_sensitive(text):
    return any(k in text for k in SENSITIVE_KW)


def is_gossip(text):
    return any(k in text for k in GOSSIP_KW)


# ---------------- GitHub API ----------------
def gh_api(path, method="GET", data=None, ok404=False):
    if not (REPO and GH_TOKEN):
        return None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(data).encode() if data else None,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise


def gh_upload(path, local_path, message):
    import base64
    if not (REPO and GH_TOKEN):
        return None
    content = base64.b64encode(open(local_path, "rb").read()).decode()
    sha = None
    cur = gh_api(f"/repos/{REPO}/contents/{urllib.parse.quote(path)}", ok404=True)
    if cur:
        sha = cur.get("sha")
    body = {"message": message, "content": content, "branch": "main"}
    if sha:
        body["sha"] = sha
    gh_api(f"/repos/{REPO}/contents/{urllib.parse.quote(path)}", "PUT", body)


# ---------------- 状态管理 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    # 旧版 seen(list) 迁移为 seen_map(dict)
    seen_map = s.get("seen_map") or {}
    for k in s.get("seen") or []:
        seen_map.setdefault(k, {"h": 0, "r": 1, "d": "2000-01-01"})
    return {"slots_done": s.get("slots_done", []),
            "last_push_at": s.get("last_push_at"),
            "seen_map": seen_map,
            "quota": s.get("quota") or {"d": "", "n": 0}}


def save_state(state):
    seen_map = dict(sorted(state["seen_map"].items(),
                           key=lambda kv: kv[1].get("d", ""))[-SEEN_KEEP:])
    payload = {"slots_done": state["slots_done"][-20:],
               "last_push_at": state["last_push_at"],
               "seen_map": seen_map,
               "quota": state["quota"]}
    local = json.dumps(payload, ensure_ascii=False, indent=1)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(local)
    if not DRY_RUN and REPO and GH_TOKEN:
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(local)
            gh_upload("state.json", tmp, "chore: update push state")
            os.remove(tmp)
        except Exception as e:
            print("state upload failed (kept locally):", type(e).__name__)


def current_slot(now_cst):
    for h in reversed(SLOT_HOURS):
        if now_cst.hour >= h:
            return now_cst.replace(hour=h, minute=0, second=0, microsecond=0)
    return None     # 窗口外（凌晨 0-5 点不推）


# ---------------- 通用抓取 ----------------
def fetch(url, timeout=25, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_text(s):
    s = html.unescape(s or "").strip()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_time(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip())
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def key_of(title):
    return re.sub(r"\s+", "", title)[:60].lower()


def translate(text, retries=2):
    if not TRANSLATE or _CJK.search(text):
        return text
    q = urllib.parse.quote(text[:500])
    url = ("https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl=en&tl=zh-CN&dt=t&q={q}")
    for i in range(retries + 1):
        try:
            data = json.loads(fetch(url, timeout=15))
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
            if out.strip():
                return out.strip()
        except Exception:
            if i < retries:
                time.sleep(1.5)
    return text


# ---------------- 微博热搜（多通道冗余） ----------------
def _norm_hot(word, heat, label="", rank=99):
    word = (word or "").strip().strip("#")
    if not word or len(word) < 2:
        return None
    try:
        heat = int(float(str(heat).replace("w", "0000").replace("万", "0000")))
    except Exception:
        heat = 0
    return dict(word=word[:40], heat=heat, label=(label or "")[:4], rank=rank)


def _weibo_official():
    raw = fetch("https://weibo.com/ajax/side/hotSearch", timeout=15,
                headers={"Referer": "https://weibo.com/"})
    data = json.loads(raw)
    out = []
    for i, it in enumerate((data.get("data") or {}).get("realtime") or []):
        if it.get("is_ad"):
            continue
        h = _norm_hot(it.get("word"), it.get("num", 0),
                      it.get("label_name", ""), i + 1)
        if h:
            out.append(h)
    return out


def _weibo_60s(host):
    raw = fetch(f"https://{host}/v2/weibo", timeout=15)
    data = json.loads(raw)
    out = []
    for i, it in enumerate(data.get("data") or []):
        h = _norm_hot(it.get("name"), it.get("hot_value", 0), "", i + 1)
        if h:
            out.append(h)
    return out


def _weibo_vvhan():
    raw = fetch("https://api.vvhan.com/api/hotlist/wbHot", timeout=15)
    data = json.loads(raw)
    out = []
    for i, it in enumerate(data.get("data") or []):
        h = _norm_hot(it.get("name"), it.get("hot", 0), "", i + 1)
        if h:
            out.append(h)
    return out


def _weibo_tenapi():
    raw = fetch("https://tenapi.cn/v2/weibohot", timeout=15)
    data = json.loads(raw)
    out = []
    for i, it in enumerate(data.get("data") or []):
        h = _norm_hot(it.get("name"), it.get("hot", 0), "", i + 1)
        if h:
            out.append(h)
    return out


def fetch_weibo_hot():
    """多通道抓微博热搜，返回 (词条列表, 失败通道名列表)。"""
    channels = [
        ("微博官方", _weibo_official),
        ("60s·viki", lambda: _weibo_60s("60s-api.viki.moe")),
        ("60s·cdn", lambda: _weibo_60s("60s.api-cdn.cn")),
        ("vvhan", _weibo_vvhan),
        ("tenapi", _weibo_tenapi),
    ]
    failed = []
    for name, fn in channels:
        try:
            out = fn()
            if len(out) >= 10:
                print(f"  [源:热搜·{name}] 抓到 {len(out)} 条热搜")
                return out, failed
            print(f"  [源:热搜·{name}] 仅 {len(out)} 条，换通道")
        except Exception as e:
            print(f"  [源:热搜·{name}] 失败: {type(e).__name__} {str(e)[:40]}")
        failed.append(name)
    # 兜底：知乎/头条热榜（60s）
    for kind in ("zhihu", "toutiao"):
        try:
            raw = fetch(f"https://60s-api.viki.moe/v2/{kind}", timeout=15)
            data = json.loads(raw)
            out = []
            for i, it in enumerate(data.get("data") or []):
                h = _norm_hot(it.get("name") or it.get("title"),
                              it.get("hot_value", 0), "", i + 1)
                if h:
                    out.append(h)
            if len(out) >= 10:
                print(f"  [源:热搜·{kind}兜底] 抓到 {len(out)} 条")
                return out, failed
        except Exception as e:
            print(f"  [源:热搜·{kind}兜底] 失败: {type(e).__name__}")
            failed.append(kind)
    return [], failed


def gn_search_title(keyword):
    """用 Google News 找该热词的媒体口径标题（二创素材）。失败返回 ''。"""
    try:
        q = urllib.parse.quote(f"{keyword} when:2d")
        url = ("https://news.google.com/rss/search?q=" + q +
               "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        root = ET.fromstring(fetch(url, timeout=15))
        for it in root.iter("item"):
            t = parse_text(it.findtext("title"))
            if t and not is_sensitive(t):
                t = re.sub(r"\s*-\s*[^-]{2,25}$", "", t)
                if keyword[:6] not in t or len(t) > len(keyword) + 8:
                    return t[:60]
    except Exception:
        pass
    return ""


# ---------------- RSS 新闻源（AI / 币圈保底） ----------------
GN_ZH = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GN_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def gn(q, zh=True):
    return (GN_ZH if zh else GN_EN).format(q=urllib.parse.quote(q))


FEEDS = [
    # AI 风向
    dict(group="ai", name="AI·中文",
         url=gn("AI OR 人工智能 OR 大模型 发布 OR 上线 OR 开源 when:1d")),
    dict(group="ai", name="AI·英文",
         url=gn("OpenAI OR Anthropic OR \"Google AI\" OR \"AI model\" launches when:1d", zh=False)),
    dict(group="ai", name="机器之心", url="https://www.jiqizhixin.com/rss"),
    # 币圈风向（中文媒体搬 X 为主）
    dict(group="crypto", name="X·BlockBeats", url="https://rss.theblockbeats.info/flash"),
    dict(group="crypto", name="X·PANews", url="https://rss.panewslab.com/zh/whatsnew"),
    dict(group="crypto", name="币圈·中文",
         url=gn("比特币 OR 以太坊 OR 加密货币 when:1d")),
    dict(group="crypto", name="CoinDesk", url="https://www.coindesk.com/arc/outboundfeeds/rss/"),
    dict(group="crypto", name="Cointelegraph", url="https://cointelegraph.com/rss"),
]


def collect_items(xml_bytes, since_dt):
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
        if pub is None or pub.tzinfo is None:
            continue
        if since_dt <= pub <= datetime.now(timezone.utc) + timedelta(hours=1):
            out.append((pub, title, link))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:MAX_PER_SOURCE]


# ---------------- 组装本档内容 ----------------
def build_items(since_dt, seen_map):
    """返回 (items, failed_list)。item: dict(group,title,link,heat,label,extra)"""
    from collections import defaultdict
    buckets = defaultdict(list)
    failed = []
    today = datetime.now(CST).strftime("%Y-%m-%d")

    # ---- 1. 全网热搜 ----
    hot_list, hot_failed = fetch_weibo_hot()
    failed.extend(hot_failed)
    gossip_used = 0
    hot_ranked = sorted(
        hot_list,
        key=lambda h: (h["label"] in ("爆", "沸"), h["heat"]), reverse=True)
    for h in hot_ranked:
        if len(buckets["hot"]) >= 6:
            break
        if is_sensitive(h["word"]):
            continue
        k = key_of(h["word"])
        old = seen_map.get(k)
        repush = False
        if old:
            surged = (h["heat"] >= old.get("h", 0) * 2 and
                      h["heat"] - old.get("h", 0) >= 300000)
            upgraded = (h["label"] in ("爆", "沸") and
                        old.get("l") not in ("爆", "沸"))
            if not ((surged or upgraded) and not old.get("r")):
                continue
            repush = True
        if is_gossip(h["word"]):
            if gossip_used >= GOSSIP_CAP:
                continue
            gossip_used += 1
        link = "https://s.weibo.com/weibo?q=" + urllib.parse.quote(f"#{h['word']}#")
        tag = h["label"] or ("热" if h["rank"] <= 5 else "")
        buckets["hot"].append(dict(
            group="hot", pub=datetime.now(timezone.utc),
            title=(f"🔥升温·{h['word']}" if repush else h["word"]),
            link=link, heat=h["heat"], label=tag, _key=k, _repush=repush))

    # ---- 2. AI / 币圈 RSS ----
    for feed in FEEDS:
        try:
            items = collect_items(fetch(feed["url"]), since_dt)
            print(f"  [源:{feed['name']}] 抓到 {len(items)} 条（窗口内）")
        except Exception as e:
            failed.append(feed["name"])
            print(f"  [源:{feed['name']}] 失败: {type(e).__name__} {str(e)[:40]}")
            continue
        for pub, title, link in items:
            if is_sensitive(title):
                continue
            k = key_of(title)
            if k in seen_map:
                continue
            t = re.sub(r"\s*-\s*[^-]{2,25}$", "", title)
            buckets[feed["group"]].append(dict(
                group=feed["group"], pub=pub, title=t[:70],
                link=link, heat=0, label="", _key=k))

    picked = buckets["hot"][:6] + buckets["ai"][:2] + buckets["crypto"][:2]
    return picked[:TOTAL_CAP], failed


# ---------------- 文案 ----------------
def _fmt_heat(n):
    if n >= 10000:
        return f"{n/10000:.0f}万"
    return str(n) if n else ""


GROUP_HEAD = {"hot": "🔥 全网热搜", "ai": "🤖 AI 风向", "crypto": "₿ 币圈风向"}


def build_tweet(items, slot_label):
    lines = [f"⚡ 今日热点速递 | {slot_label}"]
    emoji = {"hot": "🔥", "ai": "🤖", "crypto": "₿"}
    for it in items[:8]:
        t = it.get("title_zh") or it["title"]
        lines.append(f"{emoji[it['group']]} {t[:32]}")
    lines.append("👥 每日 5 档全网最热速递")
    lines.append("#热点 #热搜 #AI #Crypto")
    return "\n".join(lines)


def build_desp(items, tweet, failed, slot_label):
    parts = ["**📤 发推文案（复制即用）**", tweet, "---", "**📋 本档明细**"]
    for g in ("hot", "ai", "crypto"):
        gi = [i for i in items if i["group"] == g]
        if not gi:
            continue
        parts.append(f"\n**{GROUP_HEAD[g]}**")
        for it in gi:
            title = it.get("title_zh") or it["title"]
            heat = _fmt_heat(it.get("heat", 0))
            tag = f"`{it['label']}`" if it.get("label") else ""
            hm = f"🔥{heat}" if heat else ""
            head = " ".join(x for x in (tag, hm) if x)
            line = f"- [{title}]({it['link']}) {head}".rstrip()
            parts.append(line)
            if it.get("extra"):
                parts.append(f"  > 💬 {it['extra']}")
    if failed:
        parts.append(f"\n*部分源异常：{('、'.join(failed))[:60]}*")
    parts.append(f"\n*{slot_label} · GitHub Actions 自动推送*")
    return "\n\n".join(parts)


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
    today = now_cst.strftime("%Y-%m-%d")
    slot_dt = current_slot(now_cst)
    if FORCE and (slot_dt is None or now_cst.hour >= 12):
        # 手动强制测试：以当前时刻为临时档位
        slot_dt = now_cst.replace(minute=0, second=0, microsecond=0)
    if slot_dt is None:
        print(f"当前 {now_cst:%H:%M} 在推送窗口外（5-11点），跳过")
        return
    if now_cst.hour >= 12 and not FORCE:
        print(f"当前 {now_cst:%H:%M} 已过今日推送窗口，跳过")
        return
    slot_key = slot_dt.strftime("%Y-%m-%d_%H")
    slot_label = (f"{slot_dt.hour + DISPLAY_OFFSET}点档"
                  if slot_dt.hour in SLOT_HOURS else f"{slot_dt.hour}点档(测试)")
    state = load_state()

    if not FORCE and slot_key in state["slots_done"]:
        print(f"{slot_key} 档已推送过，本班次跳过（防重发）")
        return

    quota = state["quota"]
    if quota.get("d") != today:
        quota = {"d": today, "n": 0}
    if quota["n"] >= DAILY_QUOTA and not FORCE:
        print(f"今日已推 {quota['n']}/{DAILY_QUOTA} 条（额度保护），本档不推，"
              f"内容不标记已读，顺延处理")
        return

    if state["last_push_at"]:
        try:
            last = datetime.fromisoformat(state["last_push_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            since_dt = last.astimezone(timezone.utc) - timedelta(minutes=10)
        except Exception:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=3)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=3)
    print(f"档位: {slot_key} ({slot_label}) | 增量窗口起点(UTC): {since_dt:%Y-%m-%d %H:%M}")

    items, failed = build_items(since_dt, state["seen_map"])
    print(f"抓取完成: 精选 {len(items)} 条 | 失败源 {len(failed)}: {failed}")

    # 可靠性：热搜全挂 + RSS 大面积挂 = 基础设施故障，不消耗档位
    total_sources = 5 + len(FEEDS)
    if len(items) < MIN_ITEMS and len(failed) >= total_sources - 3:
        print("ERROR: 数据源几乎全部失败，本档不算完成，等待下个班次重试")
        sys.exit(2)
    if not items:
        # 热搜几乎不可能整小时无新词条，无内容视为异常，不静默消耗档位
        print("ERROR: 无任何可推内容，本档不标记完成，等待下个班次重试")
        sys.exit(2)

    # 二创加工：英文翻译 + 热词媒体口径
    for it in items:
        if not _CJK.search(it["title"]):
            it["title_zh"] = translate(it["title"])
            time.sleep(0.2)
        else:
            it["title_zh"] = it["title"]
        if it["group"] == "hot" and not it.get("_repush"):
            extra = gn_search_title(it["title"])
            if extra and key_of(extra) != key_of(it["title"]):
                it["extra"] = extra
            time.sleep(0.3)

    tweet = build_tweet(items, slot_label)
    desp = build_desp(items, tweet, failed, slot_label)
    title = f"全网热点速递 {now_cst:%m-%d} {slot_label}"

    print(f"== {title} == 精选 {len(items)} 条")
    print(desp[:600])
    if DRY_RUN:
        print("\n[DRY RUN] 未推送、未写状态")
        return

    if not SENDKEY:
        print("ERROR: 未配置 SENDKEY")
        sys.exit(1)

    resp = push(title, desp)
    print("PUSH RESULT:", resp[:200])
    if '"code":0' not in resp:
        print("推送失败，不写状态（下一班次自愈重试）")
        sys.exit(2)

    state["slots_done"].append(slot_key)
    state["last_push_at"] = datetime.now(CST).isoformat()
    quota["n"] += 1
    state["quota"] = quota
    for it in items:
        state["seen_map"][it["_key"]] = {
            "h": it.get("heat", 0), "l": it.get("label", ""),
            "r": 1 if it.get("_repush") else 0, "d": today}
    save_state(state)
    print(f"完成：推送成功（今日第 {quota['n']}/{DAILY_QUOTA} 条）+ 状态已回写")


if __name__ == "__main__":
    main()
