#!/usr/bin/env python3
"""
Web3 Alpha 速递（GitHub Actions 云端版 v3.3）
- 每天北京时间 4 / 7 / 10 / 13 / 16 点五档推送（对冲 GitHub 定时延迟，
  实际到达约 6 / 9 / 12 / 15 / 18 点）
- 每档只推上一档之后的新消息；状态存 state.json，经 GitHub API 回写仓库
- 五大 Alpha 板块：早期融资 / 测试网撸毛 / 预TGE新上币 / BTC-ZEC生态 / X推特风向
- 每档精选 5~8 条，带要点摘要；英文自动翻译中文
- 自动生成深色科技风海报（Pillow），上传仓库后随消息推送 raw 链接
- 附可直接复制发推的文案
仅用 Python 标准库 + Pillow（workflow 内安装）。
"""
import os
import re
import sys
import time
import html
import json
import base64
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------- 基础配置 ----------------
CST = timezone(timedelta(hours=8))
SLOT_HOURS = [4, 7, 10, 13, 16]         # 北京时间五档
FALLBACK_WINDOW = 3.5
OVERLAP_MIN = 10
MAX_PER_SOURCE = 6
SEEN_KEEP = 300
TOTAL_CAP = 10                           # 每档精选总条数上限

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SENDKEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sendkey")
if not SENDKEY and os.path.exists(SENDKEY_FILE):
    SENDKEY = open(SENDKEY_FILE, encoding="utf-8").read().strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
POSTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posters")
FONT_PATH = os.environ.get("FONT_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "font.ttf")

REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

DRY_RUN = "--dry-run" in sys.argv
FORCE = os.environ.get("PUSH_FORCE") == "1" or "--force" in sys.argv

UA = {"User-Agent": "Mozilla/5.0 (compatible; web3-daily/3.0)"}
TRANSLATE = "--no-translate" not in sys.argv
_CJK = re.compile(r"[\u4e00-\u9fff]")

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:
    PIL_OK = False

# ---------------- 数据源 ----------------
GN_ZH = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GN_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def gn(q, zh=True):
    return (GN_ZH if zh else GN_EN).format(q=urllib.parse.quote(q))


FEEDS = [
    # ---- 中文直连主通道（时效最好，带准确 pubDate）----
    dict(group="web", name="ChainCatcher", url="https://www.chaincatcher.com/rss.xml"),
    dict(group="web", name="BlockBeats", url="https://rss.theblockbeats.info/flash"),
    dict(group="web", name="PANews", url="https://rss.panewslab.com/zh/whatsnew"),
    # ---- 英文直连 ----
    dict(group="web", name="CoinDesk", url="https://www.coindesk.com/arc/outboundfeeds/rss/"),
    dict(group="web", name="Cointelegraph", url="https://cointelegraph.com/rss"),
    dict(group="web", name="Decrypt", url="https://decrypt.co/feed"),
    dict(group="web", name="TheBlock", url="https://www.theblock.co/rss.xml"),
    dict(group="web", name="DLNews", url="https://www.dlnews.com/arc/outboundfeeds/rss/"),
    # ---- BTC 生态专属通道（对标参考账号的主线）----
    dict(group="btczec", name="CT·Bitcoin", url="https://cointelegraph.com/rss/tag/bitcoin"),
    dict(group="btczec", name="BitcoinOps", url="https://bitcoinops.org/feed.xml"),
    # ---- Google News 定向补充（仅作关键词路由素材，不入「行业要闻」）----
    dict(group="raise", name="融资·中文",
         url=gn("加密 项目 完成 融资 种子轮 OR 领投 OR 战略轮 when:1d")),
    dict(group="raise", name="融资·英文",
         url=gn("crypto startup raises seed OR pre-seed round when:1d", zh=False)),
    dict(group="testnet", name="撸毛·中文",
         url=gn("测试网 空投 交互 OR 攒积分 OR 撸毛 任务 when:1d")),
    dict(group="testnet", name="撸毛·英文",
         url=gn("incentivized testnet quest points airdrop when:1d", zh=False)),
    dict(group="tge", name="TGE·快照",
         url=gn("TGE OR 代币生成 OR 快照 OR 积分 空投 when:1d")),
    dict(group="tge", name="新上币",
         url=gn("Binance Alpha OR 币安Alpha OR 新池 OR 上币 when:1d")),
    dict(group="btczec", name="BTC铭文",
         url=gn("比特币 铭文 OR 符文 OR BRC-20 OR Alkanes OR Ordinals 铸造 上线 when:1d")),
    dict(group="btczec", name="ZEC生态",
         url=gn("Zcash OR ZEC 生态 OR 升级 OR 融资 when:1d")),
    dict(group="x", name="X·KOL热议",
         url=gn("推特 KOL 加密货币 OR web3 OR 代币 喊单 when:1d")),
]

GROUPS = [
    ("btczec", "⚡ BTC 生态", 3),
    ("news", "🌐 行业要闻", 2),
    ("raise", "💰 早期融资雷达", 1),
    ("tge", "🚀 预TGE与新上币", 1),
    ("testnet", "🧪 测试网与撸毛", 1),
    ("chain", "🆕 链上新项目", 1),
    ("x", "🐦 X/推特风向", 1),
]
CHIP_LABEL = {"btczec": "BTC生态", "news": "行业要闻", "raise": "早期融资",
              "testnet": "测试网撸毛", "tge": "预TGE/新币", "chain": "链上新项目",
              "x": "X风向"}
CHIP_COLOR = {"btczec": (247, 147, 26), "news": (148, 163, 184), "chain": (236, 72, 153),
              "raise": (245, 158, 11), "testnet": (16, 185, 129), "tge": (139, 92, 246),
              "x": (56, 189, 248)}

# 关键词路由（优先级：BTCZEC > TGE > 测试网 > 融资 > 默认）
# BTC 生态是主线（对标 @CG_BRC20 的「饼子热点整理」体例）
BTCZEC_KW = ["比特币", "bitcoin", "btc", "符文", "铭文", "runes", "ordinals",
             "brc-20", "brc20", "ordinal", "zec", "zcash", "alkanes", "brc-420",
             "src-20", "arc-20", "atomicals", "stacks", "bitvm", "taproot",
             "inscription", "unisat", "铭刻", "序数", "比特币l2", "比特币二层",
             "币安链上", "btc生态", "ordinals生态"]
TGE_KW = ["tge", "代币生成", "快照", "snapshot", "上币", "listing",
          "binance alpha", "币安alpha", "launchpad", "ido", "发射"]
TESTNET_KW = ["测试网", "testnet", "交互", "quest", "积分", "points", "撸毛", "领水"]
RAISE_KW = ["融资", "raise", "raised", "seed", "种子轮", "领投", "战略轮",
            "strategic", "series a", "投资"]


def route(default_group, title):
    t = title.lower()
    if any(k in t for k in BTCZEC_KW):
        return "btczec"
    if any(k in t for k in TGE_KW):
        return "tge"
    if any(k in t for k in TESTNET_KW):
        return "testnet"
    if any(k in t for k in RAISE_KW):
        return "raise"
    return default_group


# ---- 噪音过滤：Google News 常把「印度IPO」「美股财报」这类无关文章塞进来 ----
_RELEVANT_KW = ["bitcoin", "btc", "crypto", "web3", "blockchain", "token", "defi", "nft",
                "stablecoin", "ethereum", "eth", "solana", "airdrop", "mainnet", "testnet",
                "ordinals", "runes", "brc-20", "brc20", "alkanes", "inscription", "ordinal",
                "meme", "rwa", "dex", "cex", "layer2", "l2", "rollup", "staking", "validator",
                "加密货币", "加密", "区块链", "代币", "空投", "稳定币", "以太坊", "比特币",
                "铭文", "符文", "铭刻", "链上", "交易所", "钱包", "矿工", "挖矿", "质押",
                "主网", "测试网", "山寨币", "币安", "欧易", "销毁", "清算", "链游"]
_STRONG_KW = ["bitcoin", "btc", "crypto", "web3", "blockchain", "token", "ethereum",
              "solana", "airdrop", "加密货币", "加密", "区块链", "代币", "空投", "比特币",
              "以太坊", "铭文", "符文", "链上", "稳定币", "nft", "defi", "稳定币"]
_NOISE_KW = ["ipo", "a股", "美股", "港股", "股市", "股票", "财报", "券商", "基金",
             "楼市", "房地产", "原油", "黄金价格", "关税", "选举", "足球", "nba",
             "新股申购", "上市公司", "保险", "银行股"]
# 行情播报类噪音：参考账号给的是「项目动态」，不是「巨鲸转了 0.001 枚 BTC」
_NOISE_PAT = ["多空比", "持仓量", "资金费率", "未平仓", "涨幅榜", "跌幅榜",
              "价格预测", "今日价格", "行情播报", "24小时涨", "巨鲸", "转入交易所",
              "转出交易所", "链上监测", "whale alert", "price prediction", "how to buy",
              "price forecast", "what is ", "top 5 ", "top 10 ", "best crypto",
              "一周预告", "周前瞻", "本周前瞻", "week ahead", "weekly preview"]
_NOISE_DOMAIN = ["528btc", "thaicrypto", "coingabbar", "protraderdaily", "defisummary",
                 "tapbit", "bitrue.com/blog", "beincrypto.com/price", "cryptonews.com/price"]


def is_relevant(title, desc="", link=""):
    """标题/摘要必须含加密相关词；排除股市噪音与行情播报类快讯。"""
    t = (title or "").lower()
    blob = t + " " + (desc or "").lower()
    if not any(k in blob for k in _RELEVANT_KW):
        return False
    if any(k in t for k in _NOISE_KW) and not any(k in t for k in _STRONG_KW):
        return False
    if any(p in t for p in _NOISE_PAT):
        return False
    low_link = (link or "").lower()
    if any(d in low_link for d in _NOISE_DOMAIN):
        return False
    return True


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
    """上传文件到仓库（存在则覆盖），返回国内可达的 CDN 链接。

    注意：raw.githubusercontent.com 在国内被墙，微信内置浏览器必然裂图，
    所以对外一律用 jsDelivr（国内多数网络可直连）。优先按 commit SHA 引用，
    避免分支引用被 CDN 缓存 12 小时导致新图 404。
    """
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
    resp = gh_api(f"/repos/{REPO}/contents/{urllib.parse.quote(path)}", "PUT", body) or {}
    q = urllib.parse.quote(path)
    commit_sha = ((resp.get("commit") or {}).get("sha") or "")[:12]
    if commit_sha:
        return f"https://cdn.jsdelivr.net/gh/{REPO}@{commit_sha}/{q}"
    return f"https://cdn.jsdelivr.net/gh/{REPO}@main/{q}"


def gh_cleanup_posters(keep_date):
    """删除 30 天前的海报目录，防止仓库无限膨胀。

    保留期从 7 天放宽到 30 天：以前 7 天后图片被删，微信里翻看旧消息
    就是一片 404。仓库体积可控（每天约 4 张 × 150KB）。
    """
    if not (REPO and GH_TOKEN):
        return
    try:
        cutoff = datetime.strptime(keep_date, "%Y-%m-%d") - timedelta(days=30)
        dirs = gh_api(f"/repos/{REPO}/contents/posters", ok404=True) or []
        for d in dirs:
            if d.get("type") != "dir":
                continue
            try:
                if datetime.strptime(d["name"], "%Y-%m-%d") >= cutoff:
                    continue
            except ValueError:
                continue
            for f in (gh_api(f"/repos/{REPO}/contents/{d['path']}", ok404=True) or []):
                if f.get("type") == "file":
                    try:
                        gh_api(f"/repos/{REPO}/contents/{f['path']}", "DELETE",
                               {"message": f"cleanup {f['path']}", "sha": f["sha"]})
                    except Exception:
                        pass
    except Exception as e:
        print("poster cleanup skipped:", type(e).__name__)


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
    local = json.dumps({"slots_done": state["slots_done"][-20:],
                        "last_push_at": state["last_push_at"],
                        "seen": state["seen"][-SEEN_KEEP:]}, ensure_ascii=False, indent=1)
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
    return (now_cst - timedelta(days=1)).replace(hour=SLOT_HOURS[-1], minute=0,
                                                 second=0, microsecond=0)


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
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


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


def clean_desc(raw):
    d = parse_text(raw)
    d = re.sub(r"(查看有关|View Full Article|Continue reading).*$", "", d).strip()
    return d if len(d) >= 20 else ""


def collect_items(xml_bytes, since_dt):
    """返回 [(dt, title, link, desc)]，只保留 since 之后发布的。"""
    items = []
    root = ET.fromstring(xml_bytes)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        title = parse_text(it.findtext("title"))
        link = parse_text(it.findtext("link") or "")
        pub = parse_time(it.findtext("pubDate") or it.findtext(
            "{http://purl.org/dc/elements/1.1/}date"))
        desc = clean_desc(it.findtext("description") or "")
        if title:
            items.append((pub, title, link, desc))
    for it in root.findall("atom:entry", ns):
        title = parse_text(it.findtext("atom:title", namespaces=ns))
        link_el = it.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        pub = parse_time(it.findtext("atom:updated", namespaces=ns) or
                         it.findtext("atom:published", namespaces=ns))
        desc = clean_desc(it.findtext("atom:summary", namespaces=ns) or "")
        if title:
            items.append((pub, title, link, desc))
    out = []
    for pub, title, link, desc in items:
        if pub is None or pub.tzinfo is None:
            continue
        if since_dt <= pub <= datetime.now(timezone.utc) + timedelta(hours=1):
            out.append((pub, title, link, desc))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:MAX_PER_SOURCE]


def key_of(title):
    return re.sub(r"\s+", "", title)[:60].lower()


def _dex_token_meta(addr):
    """DexScreener 代币详情：名称/符号/价格/流动性。失败返回 None。"""
    try:
        raw = fetch(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=12)
        pairs = json.loads(raw).get("pairs") or []
        if not pairs:
            return None
        p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        bt = p.get("baseToken") or {}
        return dict(name=bt.get("name", ""), symbol=bt.get("symbol", ""),
                    price=p.get("priceUsd"),
                    liq=(p.get("liquidity") or {}).get("usd"),
                    pair_url=p.get("url", ""))
    except Exception:
        return None


def _fmt_usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.4g}"


def fetch_chain_new(seen, cap=2):
    """DexScreener 最新链上代币档案 + 详情（免费无Key）。返回 (items, 是否失败)。"""
    out = []
    try:
        raw = fetch("https://api.dexscreener.com/token-profiles/latest/v1", timeout=15)
        data = json.loads(raw)
    except Exception as e:
        print(f"  [源:DexScreener] 失败: {type(e).__name__} {str(e)[:50]}")
        return out, True
    for t in data:
        if len(out) >= cap:
            break
        addr = t.get("tokenAddress", "")
        chain = t.get("chainId", "?")
        if not addr or f"dex:{addr}" in seen:
            continue
        desc = parse_text(t.get("description") or "")[:60]
        links = t.get("links") or []
        def _find(*keys):
            kl = {k.lower() for k in keys}
            for l in links:
                if str(l.get("type") or l.get("label") or "").lower() in kl:
                    return l.get("url", "")
            return ""
        meta = _dex_token_meta(addr)
        time.sleep(0.3)
        facts = [f"链: {chain}"]
        if meta:
            if meta["price"]:
                facts.append(f"价格 {_fmt_usd(meta['price'])}")
            if meta["liq"]:
                facts.append(f"流动性 {_fmt_usd(meta['liq'])}")
        facts.append(f"合约: {addr[:6]}…{addr[-4:]}")
        # 流动性过低（<$25K）基本是空气盘/rug 高发区，不进精选
        try:
            if meta and meta.get("liq") and float(meta["liq"]) < 25_000:
                print(f"    [流动性过低] {meta.get('symbol')} ${float(meta['liq']):,.0f}")
                continue
        except (TypeError, ValueError):
            pass
        # 只保留 名称+符号，不用项目自述的营销文案（机器翻译后基本是噪音）
        if meta and meta["symbol"]:
            title = f"{meta['symbol']} ({meta['name']})"
        else:
            title = f"{chain} 链新代币 {addr[:8]}…"
        link = (_find("website") or _find("twitter")
                or (meta or {}).get("pair_url")
                or f"https://dexscreener.com/{chain}/{addr}")
        out.append(dict(group="chain", pub=datetime.now(timezone.utc),
                        title=title[:80], link=link, desc="", facts=facts,
                        _key=f"dex:{addr}"))
    print(f"  [源:DexScreener] 新增链上项目 {len(out)} 个")
    return out, False


def fetch_trending():
    """CoinGecko 实时热搜榜（免费无Key）。失败返回 []。"""
    try:
        raw = fetch("https://api.coingecko.com/api/v3/search/trending", timeout=15)
        data = json.loads(raw)
        out = []
        for c in data.get("coins", [])[:5]:
            it = c["item"]
            rank = it.get("market_cap_rank")
            tag = f"{it.get('name')}({it.get('symbol')})"
            out.append(tag + (f" #{rank}" if rank else ""))
        return out
    except Exception as e:
        print(f"  [源:CoinGecko] 失败: {type(e).__name__} {str(e)[:50]}")
        return []


# ---- 详情深挖：抓原文抽关键事实 ----
_FACT_AMOUNT = [r"\$\s?[\d,.]+\s?(?:million|billion|mn|bn|m|b)\b",
                r"[\d,.]+\s?(?:万美元|万美金|亿美元|亿美金|百万美元)"]
_FACT_LEAD = [r"(?:led by|co-led by)\s+([A-Z][\w&.,' ]{2,40}?)(?:,| with| and|\.|$)",
              r"由\s*([\w一-龥A-Za-z·]{2,16}?)\s*领投",
              r"([\w一-龥A-Za-z·]{2,16}?)\s*领投"]
_FACT_DEADLINE = [r"(?:截止|截至|before|deadline|by)\s*[:：]?\s*(\d{1,2}\s?月\s?\d{1,2}\s?日|\w+ \d{1,2})",
                  r"(\d{1,2}\s?月\s?\d{1,2}\s?日)\s*(?:前|截止|领取|快照)"]
# 金额必须带语境，否则容易抓到行情价、区块高度这类无关数字
_FACT_AMT_CTX = ["融资", "轮", "投资", "估值", "价值", "募集", "拨款", "奖池", "奖励",
                 "收购", "市值", "raised", "funding", "round", "valuation", "valued",
                 "market cap", "acquisition", "worth"]
# 只有标题本身在讲钱（融资/估值/奖励），才补充金额，避免抓到行情价等无关数字
_FUND_TITLE_KW = ["融资", "轮", "投资", "估值", "募集", "收购", "拨款", "奖池",
                  "奖励", "价值", "市值", "raised", "funding", "round", "valuation",
                  "market cap"]
# 项目方社媒账号（对标参考账号：每条都标明项目方，便于自己去核）
_FACT_TW = [r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})"]


def _first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text)
        if m:
            return (m.group(1) if m.groups() else m.group(0)).strip()
    return ""


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _fact_amount(text):
    """只保留带语境（融资/估值/奖励）的金额，避免抓到行情价等无关数字。"""
    for p in _FACT_AMOUNT:
        for m in re.finditer(p, text, re.I):
            seg = text[max(0, m.start() - 60): m.end() + 40]
            if any(c in seg for c in _FACT_AMT_CTX):
                return m.group(0).strip()
    return ""


def enrich(it):
    """抓原文，抽取金额/领投/时间窗口等关键事实；失败静默保留原样。"""
    link = it.get("link") or ""
    if not link.startswith("http"):
        return
    try:
        raw = fetch(link, timeout=12)[:200_000].decode("utf-8", "ignore")
    except Exception:
        return
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = parse_text(text)[:1600]
    title = it.get("title") or ""
    title_digits = _digits(title)
    facts = []
    amt = _fact_amount(text)
    # 标题里已经写过的数字不再重复展示（参考账号的体例：只补标题没说的信息）
    if amt and _digits(amt) and _digits(amt) not in title_digits \
            and any(k in title.lower() for k in _FUND_TITLE_KW):
        facts.append(f"金额 {amt}")
    lead = _first_match(_FACT_LEAD, text)
    if lead and lead.lower() not in title.lower():
        facts.append(f"领投 {lead}")
    dl = _first_match(_FACT_DEADLINE, text)
    if dl:
        facts.append(f"窗口 {dl}")
    tw = _first_match(_FACT_TW, text)
    if tw and tw.lower() not in ("intent", "share", "home", "i", "search"):
        facts.append(f"𝕏 @{tw}")
    if facts:
        it["facts"] = facts
    if not it.get("desc"):
        m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']{25,200})',
                      raw, re.I)
        if m:
            it["desc"] = parse_text(m.group(1))[:80]


def build_items(since_dt, seen_before):
    """抓全部源 -> 路由 -> 链上新项目 -> 按板块精选。返回 (items, failed_feeds)。"""
    from collections import defaultdict
    buckets = defaultdict(list)
    seen = set(seen_before)
    failed = []
    for feed in FEEDS:
        try:
            items = collect_items(fetch(feed["url"]), since_dt)
            print(f"  [源:{feed['name']}] 抓到 {len(items)} 条（窗口内）")
        except Exception as e:
            failed.append(feed["name"])
            print(f"  [源:{feed['name']}] 失败: {type(e).__name__} {str(e)[:50]}")
            continue
        for pub, title, link, desc in items:
            k = key_of(title)
            if k in seen:
                continue
            seen.add(k)
            # 描述与标题重复时丢弃（Google News 常见情况）
            if desc and (title[:12] in desc or desc[:12] in title):
                desc = ""
            if not is_relevant(title, desc, link):
                print(f"    [噪音过滤] {title[:44]}")
                continue
            g = route(feed["group"], title)
            if g == "web":        # 直连优质源的头条进「行业要闻」；Google 源仍只作路由素材
                if feed["name"].startswith("X·") or "GoogleNews" in feed["name"]:
                    continue
                g = "news"
            buckets[g].append(dict(group=g, pub=pub, title=title,
                                   link=link, desc=desc))
    chain_items, dex_failed = fetch_chain_new(seen, cap=2)
    if dex_failed:
        failed.append("DexScreener")
    for it in chain_items:
        buckets["chain"].append(it)
    picked = []
    for g, _, cap in GROUPS:
        picked.extend(buckets[g][:cap])
    picked = picked[:TOTAL_CAP]
    return picked, failed


# ---------------- 海报生成 ----------------
def _wrap(d, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
            if len(lines) == 2:
                break
    if cur and len(lines) < 2:
        lines.append(cur)
    if len(lines) == 2 and sum(len(l) for l in lines) < len(text):
        lines[1] = lines[1][:-1] + "…"
    return lines[:2]


def make_poster(items, slot_dt, out_path):
    """深色科技风 Alpha 海报 1080x1350。失败返回 None（不影响推送）。"""
    if not PIL_OK or not os.path.exists(FONT_PATH):
        print("poster skipped: PIL/字体不可用")
        return None
    W, H = 1080, 1350
    bg, white = (10, 15, 30), (240, 244, 255)
    gray, accent = (140, 152, 180), (56, 189, 248)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    def font(sz, bold=False):
        f = ImageFont.truetype(FONT_PATH, sz)
        if bold:
            try:
                f.set_variation_by_axes([700])
            except Exception:
                pass
        return f

    f_title, f_sub = font(62, True), font(34)
    f_chip, f_item, f_detail, f_foot = font(26, True), font(33), font(25), font(24)

    d.rectangle([0, 0, W, 10], fill=accent)
    d.text((70, 70), "Web3 Alpha 速递", font=f_title, fill=white)
    sub = f"{slot_dt:%Y-%m-%d} · {slot_dt:%H}:00 档"
    tw = d.textlength(sub, font=f_sub)
    d.text((W - 70 - tw, 92), sub, font=f_sub, fill=gray)
    d.rectangle([70, 190, W - 70, 192], fill=(40, 55, 90))

    y = 240
    for it in items[:6]:
        g = it["group"]
        label = CHIP_LABEL.get(g, g)
        cw = d.textlength(label, font=f_chip) + 36
        d.rounded_rectangle([70, y, 70 + cw, y + 44], radius=22,
                            fill=CHIP_COLOR.get(g, accent))
        d.text((88, y + 8), label, font=f_chip, fill=(10, 15, 30))
        tm = it["pub"].astimezone(CST).strftime("%H:%M")
        twt = d.textlength(tm, font=f_detail)
        d.text((W - 70 - twt, y + 10), tm, font=f_detail, fill=gray)
        title = it.get("title_zh") or it["title"]
        # 去掉标题尾部的 "- 媒体名"（Google News 格式）
        title = re.sub(r"\s*-\s*\S{2,22}$", "", title) if " - " in title else title
        ty = y + 62
        for ln in _wrap(d, title, f_item, W - 140):
            d.text((70, ty), ln, font=f_item, fill=white)
            ty += 46
        det = "｜".join(it.get("facts") or []) or (it.get("desc") or "")
        if det:
            d.text((70, ty + 6), det[:44] + ("…" if len(det) > 44 else ""),
                   font=f_detail, fill=gray)
        y = ty + 62
        if y > H - 160:
            break

    d.rectangle([70, H - 110, W - 70, H - 108], fill=(40, 55, 90))
    d.text((70, H - 84), f"共 {len(items)} 条 Alpha 情报 · GitHub Actions 自动生成",
           font=f_foot, fill=gray)
    tw2 = d.textlength("非投资建议", font=f_foot)
    d.text((W - 70 - tw2, H - 84), "非投资建议", font=f_foot, fill=gray)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


# ---------------- 文案 ----------------
def build_tweet(items, slot_dt):
    lines = [f"⚡ Web3 Alpha 速递 | {slot_dt:%m-%d} {slot_dt:%H}点档"]
    emoji = {"chain": "🆕", "raise": "💰", "testnet": "🧪", "tge": "🚀",
             "btczec": "⚡", "x": "🐦", "news": "🌐"}
    for g, header, _ in GROUPS:
        gi = [i for i in items if i["group"] == g]
        if not gi:
            continue
        t = (gi[0].get("title_zh") or gi[0]["title"])
        t = re.sub(r"\s*-\s*[^-]{2,25}$", "", t)[:30]
        lines.append(f"{emoji[g]} {t}")
    lines.append("👥 关注获取每日 5 档链上情报")
    lines.append("#Web3 #Airdrop #Bitcoin #ZEC #Alpha")
    return "\n".join(lines)


def build_desp(items, poster_url, tweet, failed, trending=None):
    parts = []
    # ① 纯文字要点放最前：外部图片/链接打不开时，正文照常可读
    head = []
    for i, it in enumerate(items, 1):
        t = it.get("title_zh") or it["title"]
        extra = f"（{'｜'.join(it['facts'])}）" if it.get("facts") else ""
        head.append(f"{i}. {t}{extra}")
    parts.append("**📌 本档要点**\n\n" + "\n".join(head))
    parts.append("---")
    parts.append("**🗂 分板块明细**")
    for g, header, _ in GROUPS:
        gi = [i for i in items if i["group"] == g]
        if not gi:
            continue
        parts.append(f"\n**{header}**")
        for it in gi:
            tm = it["pub"].astimezone(CST).strftime("%H:%M")
            title = it.get("title_zh") or it["title"]
            line = f"- [{title}]({it['link']}) `{tm}`" if it["link"] else f"- {title} `{tm}`"
            parts.append(line)
            if it.get("facts"):
                parts.append(f"  > 📌 {'｜'.join(it['facts'])}")
            elif it.get("desc"):
                d = it["desc"][:70]
                parts.append(f"  > {d}{'…' if len(it['desc']) > 70 else ''}")
    if trending:
        parts.append("\n**🔥 当前市场热搜**")
        parts.append("、".join(trending))
    if failed:
        parts.append(f"\n*部分源异常：{('、'.join(failed))[:60]}*")
    parts.append("\n**📤 发推文案（复制即用）**")
    parts.append(tweet)
    if poster_url:
        parts.append("\n**🖼 海报**（图片不显示就点下面直链用浏览器打开）")
        parts.append(f"[海报原图（CDN 直链）]({poster_url})")
        parts.append(f"![海报]({poster_url})")
    parts.append("\n*GitHub Actions 自动推送 · 非投资建议*")
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
    slot_dt = current_slot(now_cst)
    slot_key = slot_dt.strftime("%Y-%m-%d_%H")
    state = load_state()

    if not FORCE and slot_key in state["slots_done"]:
        print(f"{slot_key} 档已推送过，本班次跳过（防重发）")
        return

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

    items, failed = build_items(since_dt, state["seen"])
    print(f"抓取完成: 精选 {len(items)} 条 | 失败源 {len(failed)}/{len(FEEDS)}: {failed}")

    if not items:
        # 数据源大面积失败 = 基础设施故障：不消耗档位，下个班次自动重试
        if len(failed) >= len(FEEDS) - 2:
            print("ERROR: 数据源几乎全部失败，本档不算完成，等待下个班次重试")
            sys.exit(2)
        # 距上次成功推送太久仍无新增，也视为异常（Web3 不可能几小时无新闻）
        if state["last_push_at"]:
            try:
                last = datetime.fromisoformat(state["last_push_at"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=CST)
                gap = (now_cst - last).total_seconds() / 3600
                if gap > 6:
                    print(f"ERROR: 距上次推送已 {gap:.1f} 小时却无新增，疑似异常，本档不标记完成")
                    sys.exit(2)
            except Exception:
                pass
        state["slots_done"].append(slot_key)
        if not DRY_RUN:
            save_state(state)
        print("本档确认无新增消息，不推送（不消耗额度）")
        return

    for it in items:
        if not _CJK.search(it["title"]):
            it["title_zh"] = translate(it["title"])
            time.sleep(0.2)
        else:
            it["title_zh"] = it["title"]
        if not it.get("facts"):          # 链上项目已自带事实，新闻条目做详情深挖
            enrich(it)
            time.sleep(0.3)

    trending = fetch_trending()

    poster_url = None
    poster_local = os.path.join(POSTER_DIR, slot_dt.strftime("%Y-%m-%d"),
                                f"{slot_dt:%H}.png")
    try:
        if make_poster(items, slot_dt, poster_local):
            if not DRY_RUN:
                poster_url = gh_upload(
                    f"posters/{slot_dt:%Y-%m-%d}/{slot_dt:%H}.png",
                    poster_local, f"poster {slot_key}")
            else:
                poster_url = "DRY_RUN_LOCAL"
    except Exception as e:
        print("poster failed (continue):", type(e).__name__, str(e)[:60])

    tweet = build_tweet(items, slot_dt)
    desp = build_desp(items, None if poster_url == "DRY_RUN_LOCAL" else poster_url,
                      tweet, failed, trending)
    title = f"Web3 Alpha速递 {now_cst:%m-%d} {slot_dt:%H}点档"

    print(f"== {title} == 精选 {len(items)} 条")
    print(desp[:500])
    if DRY_RUN:
        print(f"\n[DRY RUN] 海报: {poster_local} | 未推送、未写状态")
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
    state["seen"] = (state["seen"] + [it.get("_key") or key_of(it["title"])
                                      for it in items])[-SEEN_KEEP:]
    save_state(state)
    try:
        gh_cleanup_posters(slot_dt.strftime("%Y-%m-%d"))
    except Exception:
        pass
    print(f"完成：推送成功 + 海报({poster_url}) + 状态已回写")


if __name__ == "__main__":
    main()
