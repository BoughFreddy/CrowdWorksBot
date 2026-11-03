#!/usr/bin/env python3
"""
CrowdWorks new-job alert bot -> Telegram

Polls crowdworks.jp job search pages (newest first), finds jobs it has not seen
before, checks them against your keywords (title / category / description) and
sends the matching ones to Telegram.

Usage:
    python crowdworks_bot.py                 # run forever
    python crowdworks_bot.py --once          # single check (for cron / Task Scheduler)
    python crowdworks_bot.py --test          # send a test message to Telegram
    python crowdworks_bot.py --get-chat-id   # print chat IDs that messaged your bot
    python crowdworks_bot.py --dry-run       # check once, print matches, send nothing
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------- config ----
DEFAULT_URLS = [
    "https://crowdworks.jp/public/jobs/search?category_id=226&order=new",
    "https://crowdworks.jp/public/jobs/search?category_id=311&order=new",
    "https://crowdworks.jp/public/jobs/search?category_id=230&order=new",
    "https://crowdworks.jp/public/jobs/search?category_id=235&order=new",
    "https://crowdworks.jp/public/jobs/search?category_id=242&order=new",
]


def _list_env(name):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _bool_env(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _path_env(name, default):
    p = Path(os.getenv(name, "") or default)
    return p if p.is_absolute() else BASE_DIR / p


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SEARCH_URLS = _list_env("SEARCH_URLS") or DEFAULT_URLS
KEYWORDS_FILE = _path_env("KEYWORDS_FILE", "keywords.txt")
EXTRA_KEYWORDS = _list_env("KEYWORDS")            # extra keywords on top of the file
EXCLUDE_KEYWORDS = _list_env("EXCLUDE_KEYWORDS")
# false = EXCLUDE_KEYWORDS only checks title, category and client (a web job whose
# description merely mentions "SNS連携" is still alerted). true = also the description.
EXCLUDE_IN_DESCRIPTION = _bool_env("EXCLUDE_IN_DESCRIPTION", False)
CHECK_INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL", "") or 300))   # seconds, min 60
REQUEST_DELAY = max(1.0, float(os.getenv("REQUEST_DELAY", "") or 3))    # between page requests
FETCH_DETAILS = _bool_env("FETCH_DETAILS", True)  # open new jobs to read category/full text
SEEN_FILE = _path_env("SEEN_FILE", "seen_jobs.json")
ALERT_ON_FIRST_RUN = _bool_env("ALERT_ON_FIRST_RUN", False)
SHOW_SUMMARY = _bool_env("SHOW_SUMMARY", False)   # add the job description to alerts
CARD_IMAGE = _bool_env("CARD_IMAGE", True)         # send each job as a card picture
# true = screenshot the real card from CrowdWorks with a headless browser (Playwright).
# Falls back to the drawn picture if Playwright isn't installed or the capture fails.
SCREENSHOT = _bool_env("SCREENSHOT", False)
FONT_PATH = os.getenv("FONT_PATH", "").strip()           # optional: a Japanese .ttf/.ttc
FONT_BOLD_PATH = os.getenv("FONT_BOLD_PATH", "").strip()
MAX_SEEN = 10000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.8",
}
JOB_URL = "https://crowdworks.jp/public/jobs/{}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("crowdworks-bot")

session = requests.Session()
session.headers.update(HEADERS)
_last_request = 0.0


def polite_get(url):
    """GET with a minimum delay between requests to crowdworks.jp."""
    global _last_request
    wait = REQUEST_DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    try:
        r = session.get(url, timeout=30)
    finally:
        _last_request = time.time()
    if r.status_code == 429:
        raise requests.HTTPError("429 Too Many Requests (raise CHECK_INTERVAL)")
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


# -------------------------------------------------------------- keywords ----
def normalize(text):
    """Unify full/half width, case and spacing so matching is forgiving."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


_SPLIT = re.compile(r"[・/／()（）\[\]【】「」、,，\s]+")


def keyword_parts(keyword):
    """'LP（ランディングページ）制作' -> ['lp', 'ランディングページ', '制作']"""
    return [p for p in _SPLIT.split(normalize(keyword)) if p]


def load_keywords(quiet=False):
    words = []
    if KEYWORDS_FILE.exists():
        for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    elif not quiet:
        log.warning("Keywords file %s not found", KEYWORDS_FILE)
    words += EXTRA_KEYWORDS
    return list(dict.fromkeys(words))  # dedupe, keep order


def match_keywords(job, keywords):
    """Return the keywords this job matches.

    A keyword matches when
      * the job's CrowdWorks category contains it. The category is the text
        shown after the title, e.g. "<title> Webデザインの仕事の依頼" -> "Webデザイン".
        'LP（ランディングページ）制作' also matches 'LP（ランディングページ）制作・デザイン'.
      * or the whole phrase appears in the title or description,
      * or every part of it (split on ・ / （） etc.) appears in the title,
        e.g. 'iPhone・iPadアプリ開発' matches a title with 'iPhone' and 'iPadアプリ開発'.
    """
    title = normalize(job["title"])
    body = normalize(f"{job['title']} {job['summary']} {job.get('description', '')}")
    cats = [normalize(c) for c in job.get("categories", []) if c]
    heading = normalize(job.get("heading", ""))  # "<title> <category>" without の仕事の依頼
    hits = []
    for kw in keywords:
        nk = normalize(kw)
        parts = keyword_parts(kw)
        if any(nk in c for c in cats) or (heading and heading.endswith(nk)):
            hits.append(kw)
        elif nk in body:
            hits.append(kw)
        elif len(parts) > 1 and all(p in title for p in parts):
            hits.append(kw)
    return hits


def is_excluded(job):
    """True if an EXCLUDE_KEYWORDS entry matches the client's username, the title
    or the category (and the description too if EXCLUDE_IN_DESCRIPTION=true).
    Case and full/half width are ignored, so "SNS" also blocks "ｓｎｓ運用代行"."""
    client = normalize(job.get("client", ""))
    fields = [job["title"], job.get("heading", "")] + list(job.get("categories", []))
    if EXCLUDE_IN_DESCRIPTION:
        fields += [job.get("summary", ""), job.get("description", "")]
    text = normalize(" ".join(f for f in fields if f))
    for k in EXCLUDE_KEYWORDS:
        nk = normalize(k)
        if client and nk in client:
            return True
        if nk in text:
            return True
    return False


# -------------------------------------------------------------- telegram ----
def tg_api(method, files=None, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(3):
        r = requests.post(url, data=params, files=files, timeout=60)
        data = r.json()
        if data.get("ok"):
            return data["result"]
        retry = data.get("parameters", {}).get("retry_after")
        if r.status_code == 429 and retry:
            log.warning("Telegram rate limit, waiting %ss", retry)
            time.sleep(retry + 1)
            continue
        raise RuntimeError(f"Telegram error: {data.get('description')}")
    raise RuntimeError("Telegram: too many retries")


def _buttons(job_url=None, client_url=None):
    row = []
    if job_url:
        row.append({"text": "🔗 案件を見る", "url": job_url})
    if client_url:
        row.append({"text": "👤 クライアント", "url": client_url})
    return json.dumps({"inline_keyboard": [row]}) if row else None


def send_photo(png_bytes, caption, button_url=None, client_url=None):
    params = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    markup = _buttons(button_url, client_url)
    if markup:
        params["reply_markup"] = markup
    tg_api("sendPhoto", files={"photo": ("job.png", png_bytes, "image/png")}, **params)


def send_message(text):
    tg_api(
        "sendMessage",
        chat_id=CHAT_ID,
        text=text[:4096],
        parse_mode="HTML",
        disable_web_page_preview="true",
    )


# --------------------------------------------------------------- parsing ----
def _money(v):
    try:
        n = int(float(v))
        return f"{n:,}円" if n > 0 else None
    except (TypeError, ValueError):
        return None


def format_payment(payment):
    """Budget text like the job card: '固定報酬制 30,000円〜50,000円' / '固定報酬制 〜5,000円'."""
    if not isinstance(payment, dict):
        return ""
    for key, label, lo, hi, suffix in (
        ("fixed_price_payment", "固定報酬制", "min_budget", "max_budget", ""),
        ("fixed_price_writing_payment", "固定報酬制", "article_price", None, " / 記事"),
        ("hourly_payment", "時間単価制", "min_hourly_wage", "max_hourly_wage", " / 時間"),
        ("task_payment", "タスク形式", "task_price", None, " / 件"),
        ("competition_payment", "コンペ形式", "competition_price", None, ""),
    ):
        p = payment.get(key)
        if not p:
            continue
        if not isinstance(p, dict):
            return label
        a = _money(p.get(lo))
        b = _money(p.get(hi)) if hi else None
        if a and b and a != b:
            amount = f"{a}〜{b}"
        elif a:
            amount = a
        elif b:
            amount = f"〜{b}"
        else:
            amount = "金額未定" if key == "fixed_price_payment" else ""
        return f"{label} {amount}{suffix if amount and amount != '金額未定' else ''}".strip()
    return ""


def _fmt_date(v):
    """'2026-10-28T12:00:00+09:00' / '2026/10/28' / '2026年10月28日' -> date or None."""
    if not v:
        return None
    if isinstance(v, (int, float)) and v > 10**9:
        return datetime.fromtimestamp(v / 1000 if v > 10**12 else v).date()
    m = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", str(v))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def jp_date(d):
    return f"{d.year}年{d.month}月{d.day}日"


def deadline_text(d):
    """date -> 'あと 9日（11月11日まで）' like the card."""
    days = (d - date.today()).days
    if days < 0:
        return "募集終了"
    return f"あと {days}日（{d.month}月{d.day}日まで）" if days else f"本日まで（{d.month}月{d.day}日）"


def _walk(obj):
    """Yield every dict nested anywhere inside obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _category_names(*objs):
    names = []
    for o in objs:
        if not isinstance(o, dict):
            continue
        for key in ("category", "job_category", "sub_category"):
            c = o.get(key)
            if isinstance(c, dict):
                names.append(c.get("name") or c.get("title") or "")
            elif isinstance(c, str):
                names.append(c)
        for key in ("category_name", "category_title"):
            if isinstance(o.get(key), str):
                names.append(o[key])
        for c in o.get("categories") or []:
            if isinstance(c, dict):
                names.append(c.get("name") or "")
            elif isinstance(c, str):
                names.append(c)
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


_CAT_SUFFIX_RE = re.compile(r"\s*の(仕事・求人|求人・仕事|仕事の依頼|仕事|求人)\s*(一覧)?\s*$")


def clean_category(name):
    """'LP（ランディングページ）制作・デザインの仕事・求人' -> 'LP（ランディングページ）制作・デザイン'."""
    return _CAT_SUFFIX_RE.sub("", re.sub(r"\s+", " ", name or "")).strip()


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _json_extras(item):
    """Applicants, contract status, deadline, post date and PR flag from a job's
    embedded JSON. CrowdWorks doesn't document the field names, so this looks for
    likely keys anywhere inside the job's data."""
    out, applicants, contracted, capacity = {}, None, None, None
    deadline = posted = None
    for d in _walk(item):
        for k, v in d.items():
            kl = k.lower()
            if isinstance(v, (dict, list)):
                continue
            if applicants is None and re.search(r"(num|count|number)_?(of_)?(entr|appl|propos)|(entr|appl|propos)\w*_(num|count)", kl):
                applicants = _num(v)
            elif contracted is None and re.search(r"(num|count)_?(of_)?contract|contract\w*_(num|count)|contracted", kl) \
                    and "hope" not in kl and "limit" not in kl:
                contracted = _num(v)
            elif capacity is None and re.search(r"hope_number|recruit\w*(num|count)|capacity|num_of_hope|wanted|limit_num", kl):
                capacity = _num(v)
            elif deadline is None and re.search(r"expire|deadline|close[sd]?_(on|at)|end_(on|at)|apply_until", kl):
                deadline = _fmt_date(v)
            elif posted is None and re.search(r"released_at|published_at|posted_at|opened_at|start_(on|at)", kl):
                posted = _fmt_date(v)
            elif kl in ("pr", "is_pr", "pr_flag", "is_promoted", "promoted", "sponsored", "is_sponsored",
                        "pr_job", "is_pr_job", "advertised", "is_advertisement") and v:
                out["pr"] = True
    if applicants is not None:
        out["applicants"] = f"{applicants}人"
    if contracted is not None and capacity:
        out["contracts"] = f"{contracted}/{capacity}人"
    if deadline:
        out["deadline_date"] = deadline
    if posted:
        out["posted"] = jp_date(posted)
    return out


_DEBUG_ITEM = None


def _json_blobs(soup):
    """All JSON payloads embedded in the page (data attributes / JSON scripts)."""
    raws = []
    for tag in soup.find_all(attrs={"data": True}):
        raw = tag.get("data", "")
        if raw.lstrip().startswith(("{", "[")):
            raws.append(raw)
    for tag in soup.find_all("script"):
        t = (tag.get("type") or "").lower()
        if "json" in t or tag.get("id") == "__NEXT_DATA__":
            raws.append(tag.string or tag.get_text() or "")
    out = []
    for raw in raws:
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return out


JOB_SUFFIX = "の仕事の依頼"
_SUFFIX_RE = re.compile(r"\s*の仕事の依頼\s*(?:[|｜\-–—].*)?$")


def split_title_category(text, known_title=None):
    """'<title> Webデザインの仕事の依頼' -> ('<title>', 'Webデザイン').

    Returns (text, '') when the text doesn't end with 'の仕事の依頼'.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    m = _SUFFIX_RE.search(text)
    if not m:
        return text, ""
    head = text[:m.start()].strip()
    if known_title:
        kt = re.sub(r"\s+", " ", known_title).strip()
        if head.startswith(kt) and len(head) > len(kt):
            return kt, head[len(kt):].strip()
    # A keyword that contains a space ("スマートフォン アプリ開発・サイト構築")
    # may be the whole category; otherwise the category is the last word.
    nhead = normalize(head)
    for kw in sorted(load_keywords(quiet=True), key=len, reverse=True):
        nk = normalize(kw)
        if " " in nk and nhead.endswith(" " + nk) and len(nhead) == len(head):
            return head[:-len(nk)].strip(), head[-len(nk):]
    if " " not in head:
        return head, ""
    title, category = head.rsplit(" ", 1)
    return title.strip(), category.strip()


def _card_for(a, job_id):
    """Smallest ancestor of a job link that contains only this one job."""
    node, card = a, a
    for _ in range(8):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        other = {m.group(1) for x in node.find_all("a", href=True)
                 for m in [re.search(r"/public/jobs/(\d+)", x["href"])] if m}
        if other - {job_id}:
            break
        card = node
    return card


SITE = "https://crowdworks.jp"


def _img_src(img):
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return urljoin(SITE, v)
    return ""


def _client_info(node):
    """(username, profile URL, profile image URL) from links to /public/employers/<id>."""
    name = url = avatar = ""
    for x in node.find_all("a", href=True):
        if not re.search(r"/public/employers/\d+", x["href"]):
            continue
        url = url or urljoin(SITE, x["href"].split("?")[0])
        t = x.get_text(" ", strip=True)
        if t and not name:
            name = t
        img = x.find("img")
        if img and not avatar:
            avatar = _img_src(img)
            name = name or (img.get("alt") or "").strip()
    return name, url, avatar


def _client_in(node):
    return _client_info(node)[0]


def _json_client(client):
    """(username, profile URL, profile image URL) from a JSON client object."""
    if not isinstance(client, dict):
        return "", "", ""
    name = str(client.get("username") or client.get("name") or "")
    cid = client.get("id") or client.get("user_id") or client.get("employer_id")
    url = f"{SITE}/public/employers/{cid}" if cid else ""
    avatar = ""
    for d in _walk(client):
        for k, v in d.items():
            if isinstance(v, str) and re.search(r"image|logo|avatar|icon|thumb|photo|picture|img", k.lower()) \
                    and re.search(r"^(https?:)?//|^/", v) and not avatar:
                avatar = urljoin(SITE, v)
    return name, url, avatar


_BUDGET_RE = re.compile(
    r"(固定報酬制|時間単価制|タスク|コンペ)?\s*"
    r"((?:[\d,]+\s*円\s*)?[～〜~]\s*[\d,]+\s*円|[\d,]+\s*円(?:\s*[～〜~]\s*(?:[\d,]+\s*円)?)?)")


def _card_category(card):
    """Category shown on the card (📁 Webデザイン), i.e. a link to a category page."""
    for x in card.find_all("a", href=True):
        if re.search(r"/public/jobs/(category|group)/|category_id=|/category/", x["href"]):
            t = x.get_text(" ", strip=True)
            if t and len(t) < 60:
                return t
    return ""


def _card_budget(card_text):
    m = _BUDGET_RE.search(card_text)
    if not m:
        return ""
    kind, amount = m.group(1) or "", re.sub(r"\s+", "", m.group(2))
    amount = amount.replace("~", "〜").replace("～", "〜")
    return f"{kind} {amount}".strip()


def _card_extras(card, card_text):
    """Applicants, contract status, deadline, post date and PR mark from a card."""
    t = re.sub(r"\s+", " ", card_text)
    out = {}
    m = re.search(r"応募数\s*([\d,]+)\s*人", t)
    if m:
        out["applicants"] = f"{m.group(1)}人"
    m = re.search(r"契約状況\s*([\d,]+\s*/\s*[\d,]+\s*人)", t)
    if m:
        out["contracts"] = re.sub(r"\s+", "", m.group(1))
    m = re.search(r"あと\s*(\d+\s*(?:日|時間|分))\s*(?:[(（]\s*([^)）]+?)\s*[)）])?", t)
    if m:
        out["deadline"] = re.sub(r"\s+", "", m.group(1)) + (f"（{m.group(2)}）" if m.group(2) else "")
    elif "募集終了" in t:
        out["deadline"] = "募集終了"
    m = re.search(r"掲載日\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)", t)
    if m:
        out["posted"] = m.group(1)
    if "認定クライアント" in t:
        out["certified"] = True
    out["pr"] = bool(re.search(r"(^|\s)PR(\s|$)", t)) or bool(
        card.find(string=lambda x: x and x.strip() == "PR"))
    return out


def parse_cards(soup):
    """Read the job cards on a search page.

    Each card shows the title (link to /public/jobs/<id>), the category
    (📁 Webデザイン), the budget and the client's username, so no job page
    needs to be opened.
    """
    by_id, raw_len = {}, {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/public/jobs/(\d+)/?(?:\?.*)?$", a["href"])
        text = a.get_text(" ", strip=True)
        if not m or not text:
            continue
        jid = m.group(1)
        if raw_len.get(jid, -1) >= len(text):
            continue  # keep the longest link text (the title link)
        raw_len[jid] = len(text)
        card = _card_for(a, jid)
        card_text = card.get_text(" ", strip=True)

        heading = text
        if JOB_SUFFIX not in heading:  # "<title> Webデザインの仕事の依頼" variant
            for el in card.find_all(True):
                t = el.get_text(" ", strip=True)
                if t.endswith(JOB_SUFFIX) and len(t) < 300:
                    heading = t
                    break
        title, suffix_cat = split_title_category(heading)
        cats = [c for c in (_card_category(card), suffix_cat) if c]
        by_id[jid] = {
            "id": jid, "title": title, "url": JOB_URL.format(jid),
            "budget": _card_budget(card_text) if card is not a else "",
            "client": _client_info(card)[0],
            "client_url": _client_info(card)[1],
            "avatar_url": _client_info(card)[2],
            "summary": "",
            "categories": list(dict.fromkeys(cats)),
            "heading": _SUFFIX_RE.sub("", heading) if JOB_SUFFIX in heading else "",
            **(_card_extras(card, card_text) if card is not a else {}),
        }
    return list(by_id.values())


def parse_jobs(page_html):
    """Return jobs on a search page: {id, title, url, budget, client, summary, categories}."""
    global _DEBUG_ITEM
    soup = BeautifulSoup(page_html, "html.parser")
    cards = parse_cards(soup)
    jobs, ids = [], set()

    # 1) The search page may embed its results as JSON (id="vue-container" data="...").
    for data in _json_blobs(soup):
        for d in _walk(data):
            offer = d.get("job_offer")
            if not (isinstance(offer, dict) and offer.get("id") and offer.get("title")):
                continue
            jid = str(offer["id"])
            if jid in ids:
                continue
            ids.add(jid)
            client = d.get("client") or {}
            jobs.append({
                "id": jid,
                "title": str(offer.get("title", "")).strip(),
                "url": JOB_URL.format(jid),
                "budget": format_payment(d.get("payment")),
                "client": _json_client(client)[0],
                "client_url": _json_client(client)[1],
                "avatar_url": _json_client(client)[2],
                "summary": str(offer.get("description_digest") or offer.get("description") or "").strip(),
                "categories": [clean_category(c) for c in _category_names(offer, d)],
                **_json_extras(d),
            })
            if _DEBUG_ITEM is None:
                _DEBUG_ITEM = d

    # 2) Otherwise use the HTML cards; if both exist, fill gaps in the JSON from the cards.
    if not jobs:
        return cards
    by_card = {c["id"]: c for c in cards}
    for job in jobs:
        c = by_card.get(job["id"])
        if not c:
            continue
        job["categories"] = list(dict.fromkeys(job["categories"] + c["categories"]))
        job["client"] = job["client"] or c["client"]
        job["client_url"] = job.get("client_url") or c.get("client_url", "")
        job["avatar_url"] = job.get("avatar_url") or c.get("avatar_url", "")
        job["budget"] = job["budget"] or c["budget"]
        for k in ("heading", "applicants", "contracts", "deadline", "posted", "pr"):
            if c.get(k) and not job.get(k):
                job[k] = c[k]
    return jobs


def _stats_from_text(t):
    """Stats as printed on a CrowdWorks job page or card."""
    t = re.sub(r"\s+", " ", t)
    out = {}
    m = re.search(r"(?:応募した人|応募数|応募者)\s*[:：]?\s*([\d,]+)\s*人", t)
    if m:
        out["applicants"] = f"{m.group(1)}人"
    m = re.search(r"契約状況\s*([\d,]+)\s*/\s*([\d,]+)\s*人", t)
    if m:
        out["contracts"] = f"{m.group(1)}/{m.group(2)}人"
    else:
        c = re.search(r"契約した人\s*[:：]?\s*([\d,]+)\s*人", t)
        n = re.search(r"募集人数\s*[:：]?\s*([\d,]+)\s*人", t)
        if c and n:
            out["contracts"] = f"{c.group(1)}/{n.group(1)}人"
    m = re.search(r"(?:応募期限|募集期限|募集終了日)\s*[:：]?\s*(\d{4}年\s*\d{1,2}月\s*\d{1,2}日)", t)
    if m:
        out["deadline_date"] = _fmt_date(m.group(1))
    m = re.search(r"掲載日\s*[:：]?\s*(\d{4}年\s*\d{1,2}月\s*\d{1,2}日)", t)
    if m:
        out["posted"] = jp_date(_fmt_date(m.group(1)))
    m = re.search(r"(固定報酬制|時間単価制|タスク形式|コンペ形式)", t)
    if m:
        out["pay_type"] = m.group(1)
    return out


def parse_job_detail(page_html, known_title=None):
    """Read category, client, stats and description from a job's own page.

    The job page heading looks like "<title> Webデザインの仕事の依頼"; the part
    after the title is the job's own (sub-)category. Parent groups such as
    "ホームページ制作・Webデザイン" are NOT used for keyword matching, otherwise
    every job in the group would match "Webデザイン".
    """
    soup = BeautifulSoup(page_html, "html.parser")
    categories, description, heading = [], "", ""

    candidates = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])]
    og = soup.find("meta", attrs={"property": "og:title"})
    if og:
        candidates.append(og.get("content", ""))
    if soup.title:
        candidates.append(soup.title.get_text(" ", strip=True))
    for text in candidates:
        if JOB_SUFFIX in (text or ""):
            _, cat = split_title_category(text, known_title)
            if cat:
                categories.append(cat)
            heading = _SUFFIX_RE.sub("", re.sub(r"\s+", " ", text)).strip()
            break

    # Sub-category links only, e.g. <a href="/public/jobs/category/14">LP（ランディングページ）制作・デザイン</a>
    for a in soup.find_all("a", href=True):
        if re.search(r"/public/jobs/category/\w+", a["href"]):
            t = clean_category(a.get_text(" ", strip=True))
            if t and len(t) < 60:
                categories.append(t)

    client, client_url, avatar_url = _client_info(soup)
    for data in _json_blobs(soup):
        for d in _walk(data):
            desc = d.get("description")
            if isinstance(desc, str) and len(desc) > len(description):
                description = desc
            c = d.get("client")
            if not client and isinstance(c, dict) and c.get("username"):
                client = str(c["username"])

    if not description:
        node = soup.select_one(".job_offer_detail_table, [class*='description'], main")
        if node:
            description = node.get_text(" ", strip=True)
    if not description:
        meta = soup.find("meta", attrs={"name": "description"})
        description = meta.get("content", "") if meta else ""

    return {
        "categories": list(dict.fromkeys(clean_category(c) for c in categories if c)),
        "description": description[:5000],
        "client": client,
        "client_url": client_url,
        "avatar_url": avatar_url,
        "heading": heading,
        **_stats_from_text(soup.get_text(" ", strip=True)),
    }


def fetch_jobs(url):
    return parse_jobs(polite_get(url))


def enrich_job(job):
    """Add category / client / full description from the job page (best effort)."""
    try:
        d = parse_job_detail(polite_get(job["url"]), job["title"])
    except requests.RequestException as e:
        log.warning("Could not open %s: %s", job["url"], e)
        return
    job["categories"] = list(dict.fromkeys(job.get("categories", []) + d["categories"]))
    job["description"] = d["description"]
    job["client"] = job.get("client") or d["client"]
    job["client_url"] = job.get("client_url") or d.get("client_url", "")
    job["avatar_url"] = job.get("avatar_url") or d.get("avatar_url", "")
    job["heading"] = job.get("heading") or d["heading"]
    for k in ("applicants", "contracts", "deadline_date", "posted"):
        if d.get(k) and not job.get(k):
            job[k] = d[k]
    if d.get("pay_type") and job.get("budget") and not re.match(r"(固定|時間|タスク|コンペ)", job["budget"]):
        job["budget"] = f"{d['pay_type']} {job['budget']}"
    job["_enriched"] = True


def format_job(job, hits):
    """Telegram message laid out like the CrowdWorks job card."""
    e = html.escape
    head = "🆕 " + ("[PR] " if job.get("pr") else "[新着] " if job.get("is_new") else "") \
        + ("[認定クライアント] " if job.get("certified") else "")
    lines = [f"{head}<b>{e(job['title'])}</b>"]
    if job.get("categories"):
        lines.append(f"📂 {e(job['categories'][0])}")  # the job's own category, as on the card
    if job.get("budget"):
        lines.append(f"💴 {e(job['budget'])}")
    stats = []
    if job.get("applicants"):
        stats.append(f"応募数 {job['applicants']}")
    if job.get("contracts"):
        stats.append(f"契約状況 {job['contracts']}")
    if stats:
        lines.append("👥 " + e(" ｜ ".join(stats)))
    if job.get("deadline_date"):
        lines.append(f"⏳ {e(deadline_text(job['deadline_date']))}")
    elif job.get("deadline"):
        d = job["deadline"]
        lines.append(f"⏳ {e(d if d == '募集終了' else 'あと ' + d)}")
    if job.get("posted"):
        lines.append(f"📅 掲載日：{e(job['posted'])}")
    if job.get("client"):
        if job.get("client_url"):
            lines.append(f'👤 <a href="{e(job["client_url"])}">{e(job["client"])}</a>')
        else:
            lines.append(f"👤 {e(job['client'])}")
    lines.append(f"🔑 {e(', '.join(hits[:5]))}")
    s = job.get("summary") or job.get("description", "")
    if SHOW_SUMMARY and s:
        s = re.sub(r"\s+", " ", s)
        lines.append(f"\n{e(s[:300] + ('…' if len(s) > 300 else ''))}")
    lines.append(f"🔗 {job['url']}")
    return "\n".join(lines)


# ------------------------------------------------------------- card image ----
_FONT_CANDIDATES = [
    # Windows
    ("C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/meiryob.ttc"),
    ("C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/YuGothB.ttc"),
    ("C:/Windows/Fonts/msgothic.ttc", None),
    # macOS
    ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", None),
    # Linux
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", None),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None),
]
_font_cache = {}


def _font_paths():
    if FONT_PATH:
        return FONT_PATH, FONT_BOLD_PATH or None
    for reg, bold in _FONT_CANDIDATES:
        if os.path.exists(reg):
            return reg, bold if bold and os.path.exists(bold) else None
    return None, None


def _font(size, bold=False):
    """(font, fake_bold) at the given pixel size, or raises if no Japanese font."""
    from PIL import ImageFont
    key = (size, bold)
    if key not in _font_cache:
        reg, bld = _font_paths()
        if not reg:
            raise RuntimeError("No Japanese font found; set FONT_PATH in .env")
        path = bld if (bold and bld) else reg
        _font_cache[key] = (ImageFont.truetype(path, size), bold and not bld)
    return _font_cache[key]


_EMOJI_RE = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]")

BLUE, RED, GRAY, DARK = (32, 84, 156), (200, 16, 30), (120, 120, 120), (51, 51, 51)
LINE, GREEN, PILL_FIXED, PILL_HOURLY = (205, 205, 205), (118, 181, 47), (108, 158, 196), (140, 191, 74)


def _load_avatar(url, size):
    """Download the client's profile image and return it as a size x size RGB image."""
    if not url:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        r = session.get(url, timeout=15)
        r.raise_for_status()
        im = Image.open(BytesIO(r.content)).convert("RGBA")
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        im = im.resize((size, size), Image.LANCZOS)
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[3])
        return bg
    except Exception as e:
        log.info("Could not load profile image %s: %s", url, e)
        return None


def render_card(job, scale=2):
    """Draw the job like a CrowdWorks search-result card. Returns PNG bytes."""
    from io import BytesIO
    from PIL import Image, ImageDraw

    S = scale
    W = 820
    certified = bool(job.get("certified"))
    top = 30 if certified else 0
    H = top + 400  # cropped to the real height at the end
    img = Image.new("RGB", (W * S, H * S), "white")
    d = ImageDraw.Draw(img)

    def text(xy, t, size, fill=DARK, bold=False, anchor="la"):
        f, fake = _font(size * S, bold)
        d.text((xy[0] * S, xy[1] * S), t, font=f, fill=fill, anchor=anchor,
               stroke_width=(1 if fake else 0), stroke_fill=fill)

    def width(t, size, bold=False):
        f, _ = _font(size * S, bold)
        return d.textlength(t, font=f) / S

    def vline(x, y1, y2):
        for y in range(int(y1), int(y2), 4):
            d.line([(x * S, y * S), (x * S, (y + 2) * S)], fill=(150, 150, 150), width=S)

    x0, y = 18, top + 14
    # badge
    if job.get("pr"):
        d.rectangle([x0 * S, y * S, (x0 + 34) * S, (y + 18) * S], outline=RED, width=S + 1)
        text((x0 + 17, y + 9), "PR", 12, RED, True, "mm")
        y += 26
    elif job.get("is_new"):
        d.rectangle([x0 * S, y * S, (x0 + 40) * S, (y + 19) * S], fill=GREEN)
        text((x0 + 20, y + 9.5), "新着", 12, "white", True, "mm")
        y += 27

    # title (max 2 lines, left column)
    col_left_w = 420
    title = _EMOJI_RE.sub("", job.get("title", "")).strip()
    lines, cur = [], ""
    for ch in title:
        if width(cur + ch, 16, True) > col_left_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        last = lines[1]
        while last and width(last + "…", 16, True) > col_left_w:
            last = last[:-1]
        lines = [lines[0], last + "…"]
    title_top = y
    for ln in lines:
        text((x0, y), ln, 16, BLUE, True)
        y += 25
    # category with folder icon
    y += 6
    if job.get("categories"):
        fx, fy = x0, y + 2
        d.rectangle([fx * S, (fy + 2) * S, (fx + 14) * S, (fy + 12) * S], fill=(35, 70, 130))
        d.rectangle([fx * S, fy * S, (fx + 6) * S, (fy + 3) * S], fill=(35, 70, 130))
        text((x0 + 20, y), job["categories"][0], 12, BLUE)
    y += 22
    col_bottom = max(y, title_top + 80)

    # columns
    cx = [455, 590, 705, W - 12]
    for x in cx[:3]:
        vline(x, title_top - 2, col_bottom)

    # pay column
    budget = job.get("budget", "")
    label, amount = (budget.split(" ", 1) + [""])[:2] if " " in budget else ("", budget)
    mid = (cx[0] + cx[1]) / 2
    cy = title_top + 8
    if label:
        lw = width(label, 11, True) + 16
        color = PILL_HOURLY if "時間" in label else PILL_FIXED
        d.rounded_rectangle([(mid - lw / 2) * S, cy * S, (mid + lw / 2) * S, (cy + 18) * S], 3 * S, fill=color)
        text((mid, cy + 9), label, 11, "white", True, "mm")
    cy += 26
    amount = re.sub(r"\s*/\s*(時間|件|記事)$", "", amount).replace("〜", "～")
    parts = [amount]
    if width(amount, 15, True) > (cx[1] - cx[0] - 10) and "～" in amount:
        a, b2 = amount.split("～", 1)
        parts = [a.strip() + " ～", b2.strip()] if a.strip() else ["～ " + b2.strip()]
    for ptxt in parts:
        text((mid, cy), ptxt, 15, RED, True, "ma")
        cy += 22

    # applicants column
    mid = (cx[1] + cx[2]) / 2
    n = re.sub(r"人$", "", job.get("applicants", "") or "-")
    t1, t2, t3 = "応募数 ", n, " 人"
    w = width(t1, 13) + width(t2, 17, True) + width(t3, 13)
    xx = mid - w / 2
    text((xx, title_top + 24), t1, 13); xx += width(t1, 13)
    text((xx, title_top + 20), t2, 17, DARK, True); xx += width(t2, 17, True)
    text((xx, title_top + 24), t3, 13)
    if job.get("contracts"):
        text((mid, title_top + 48), f"契約状況  {job['contracts']}", 10, GRAY, False, "ma")

    # deadline column
    mid = (cx[2] + cx[3]) / 2
    dd = job.get("deadline_date")
    if dd:
        days = (dd - date.today()).days
        if days < 0:
            text((mid, title_top + 24), "募集終了", 13, GRAY, False, "ma")
        else:
            t1, t2, t3 = "あと ", str(days), " 日"
            w = width(t1, 13) + width(t2, 17, True) + width(t3, 13)
            xx = mid - w / 2
            text((xx, title_top + 24), t1, 13); xx += width(t1, 13)
            text((xx, title_top + 20), t2, 17, DARK, True); xx += width(t2, 17, True)
            text((xx, title_top + 24), t3, 13)
            text((mid, title_top + 48), f"({dd.month}月{dd.day}日まで)", 11, DARK, False, "ma")
    elif job.get("deadline"):
        text((mid, title_top + 24), job["deadline"], 12, DARK, False, "ma")

    # bottom row: client + post date
    H = max(col_bottom, cy) + 52
    by = H - 34
    if job.get("client"):
        avatar = _load_avatar(job.get("avatar_url"), 24 * S)
        if avatar is not None:
            img.paste(avatar, (x0 * S, (by - 4) * S))
        else:
            d.rectangle([x0 * S, (by - 4) * S, (x0 + 24) * S, (by + 20) * S], fill=(225, 225, 225))
            d.ellipse([(x0 + 7) * S, (by) * S, (x0 + 17) * S, (by + 10) * S], fill="white")
            d.pieslice([(x0 + 3) * S, (by + 10) * S, (x0 + 21) * S, (by + 28) * S], 180, 360, fill="white")
        text((x0 + 32, by), _EMOJI_RE.sub("", job["client"]), 13, BLUE)
    if job.get("posted"):
        text((W - 18, by), f"掲載日： {job['posted']}", 12, GRAY, False, "ra")

    # frame
    img = img.crop((0, 0, W * S, H * S))
    d = ImageDraw.Draw(img)
    if certified:
        d.rectangle([0, top * S, W * S - 1, H * S - 1], outline=(40, 110, 190), width=2 * S)
        d.rectangle([0, 0, 150 * S, top * S], fill=(40, 110, 190))
        text((12, 7), "認定クライアント", 13, "white", True)
    else:
        d.rectangle([0, 0, W * S - 1, H * S - 1], outline=LINE, width=S)

    out = BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


# ------------------------------------------------------ real screenshots ----
_MARK_CARDS_JS = r"""
(ids) => {
  const re = /\/public\/jobs\/(\d+)\/?(\?.*)?$/;
  const idOf = (a) => ((a.getAttribute('href') || '').match(re) || [])[1];
  // Hide fixed/sticky headers, banners and chat widgets so they can't cover a card.
  for (const el of document.querySelectorAll('body *')) {
    const pos = getComputedStyle(el).position;
    if (pos === 'fixed' || pos === 'sticky') el.style.setProperty('display', 'none', 'important');
  }
  const found = [];
  for (const id of ids) {
    const links = [...document.querySelectorAll('a[href]')].filter(a => idOf(a) === id);
    if (!links.length) continue;
    links.sort((x, y) => y.textContent.length - x.textContent.length);
    let card = links[0], node = links[0];
    for (let i = 0; i < 12; i++) {
      node = node.parentElement;
      if (!node || node.tagName === 'BODY' || node.tagName === 'MAIN') break;
      const others = [...node.querySelectorAll('a[href]')].map(idOf).filter(v => v && v !== id);
      if (others.length) break;   // this element already contains another job
      card = node;
    }
    card.setAttribute('data-cwbot-card', id);
    found.push(id);
  }
  return found;
}
"""


def capture_cards(url_to_ids):
    """Open each search page in a headless browser and screenshot the cards of the
    given jobs. Returns ({job_id: png_bytes}, {url: rendered_html})."""
    from playwright.sync_api import sync_playwright
    shots, rendered = {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 1600}, device_scale_factor=2,
                locale="ja-JP", user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            for url, ids in url_to_ids.items():
                try:
                    wait = REQUEST_DELAY - (time.time() - _last_request)
                    if wait > 0:
                        time.sleep(wait)
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_selector('a[href*="/public/jobs/"]', timeout=20000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    rendered[url] = page.content()
                    for jid in page.evaluate(_MARK_CARDS_JS, list(ids)):
                        try:
                            loc = page.locator(f'[data-cwbot-card="{jid}"]').first
                            loc.scroll_into_view_if_needed(timeout=5000)
                            shots[jid] = loc.screenshot(timeout=10000)
                        except Exception as e:
                            log.warning("Screenshot of job %s failed: %s", jid, e)
                except Exception as e:
                    log.warning("Browser could not load %s: %s", url, e)
        finally:
            browser.close()
    log.info("Captured %d card screenshot(s)", len(shots))
    return shots, rendered


def send_job(job, hits):
    """Send a job alert: a card picture with a short caption and a button,
    falling back to the text alert if the picture can't be made."""
    if CARD_IMAGE:
        try:
            png = job.get("_shot") or render_card(job)
            caption = (f"<b>{html.escape(job['title'])}</b>\n"
                       f"🔑 {html.escape(', '.join(hits[:5]))}\n{job['url']}")
            send_photo(png, caption, job["url"], job.get("client_url"))
            return
        except ImportError:
            log.warning("Pillow is not installed (pip install pillow); sending text alert")
        except Exception as e:
            log.warning("Card image failed (%s); sending text alert", e)
    send_message(format_job(job, hits))


# ------------------------------------------------------------ seen store ----
# Every ID handled while the bot is running, kept in memory too. Even if Windows
# (antivirus, OneDrive, an editor, a second copy of the bot...) blocks the file for
# a while, jobs are not alerted twice.
_mem_seen = []


def load_seen():
    """Seen job IDs (file + memory), or None if the bot has never run."""
    if not SEEN_FILE.exists():
        return list(_mem_seen) if _mem_seen else None
    for attempt in range(10):
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            ids = [str(x) for x in data] if isinstance(data, list) else []
            return list(dict.fromkeys(ids + _mem_seen))
        except json.JSONDecodeError as e:
            log.error("%s is damaged (%s); keeping a backup and starting a new list", SEEN_FILE, e)
            try:
                SEEN_FILE.replace(SEEN_FILE.with_name(SEEN_FILE.stem + ".broken.json"))
            except OSError:
                pass
            return list(_mem_seen)
        except OSError as e:  # file locked by another program: wait and retry
            if attempt == 9:
                log.warning("Could not read %s (%s); using the in-memory list", SEEN_FILE, e)
                return list(_mem_seen)
            time.sleep(min(0.2 * 2 ** attempt, 3))


def save_seen(seen):
    """Save seen IDs. Never raises: on failure the IDs stay in memory and the
    file is written again on the next save."""
    global _mem_seen
    _mem_seen = list(dict.fromkeys(_mem_seen + list(seen)))[-MAX_SEEN:]
    data = json.dumps(_mem_seen)
    tmp = SEEN_FILE.with_name(SEEN_FILE.name + ".tmp")
    last_err = None
    for attempt in range(10):
        try:
            tmp.write_text(data, encoding="utf-8")
            try:
                os.replace(tmp, SEEN_FILE)  # atomic swap
            except PermissionError:
                # Windows can refuse the rename while something has the file open;
                # writing the contents in place usually still works.
                SEEN_FILE.write_text(data, encoding="utf-8")
            return True
        except OSError as e:
            last_err = e
            time.sleep(min(0.2 * 2 ** attempt, 3))  # up to ~15 s in total
    log.warning("Could not save %s (%s). Is it open in another program, or is a second "
                "copy of the bot running? Will retry on the next save.", SEEN_FILE, last_err)
    return False


# ------------------------------------------------------- single instance ----
_lock_handle = None


def acquire_single_instance_lock():
    """Stop a second copy of the bot from running in the same folder."""
    global _lock_handle
    lock_path = SEEN_FILE.with_name("crowdworks_bot.lock")
    _lock_handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another copy of crowdworks_bot.py is already running in this folder. "
                 "Close it first (check other terminal windows / Task Scheduler).")


# ------------------------------------------------------------------ main ----
def _apply_screenshots(jobs, url):
    """Optional (SCREENSHOT=true): real card screenshots + card data from a browser."""
    try:
        shots, rendered = capture_cards({url: [j["id"] for j in jobs]})
        cards = {}
        for page_html in rendered.values():
            for c in parse_cards(BeautifulSoup(page_html, "html.parser")):
                cards[c["id"]] = c
        for job in jobs:
            if job["id"] in shots:
                job["_shot"] = shots[job["id"]]
            c = cards.get(job["id"])
            if c:
                job["categories"] = list(dict.fromkeys(c["categories"] + job.get("categories", [])))
                for k in ("client", "client_url", "avatar_url", "budget", "applicants", "contracts",
                          "deadline", "posted", "pr", "certified", "heading"):
                    if c.get(k) and not job.get(k):
                        job[k] = c[k]
    except ImportError:
        log.warning("Playwright not installed; using drawn cards "
                    "(pip install playwright && python -m playwright install chromium)")
    except Exception as e:
        log.warning("Screenshot step failed (%s); using drawn cards", e)


def _handle_job(job, keywords, seen_list, dry_run):
    """Check one new job and alert it right away if it matches."""
    # Mark as seen and save FIRST, so this job can never be alerted again.
    if not dry_run:
        seen_list.append(job["id"])
        save_seen(seen_list)
    try:
        # The category is normally on the search page. Only open the job page
        # if it wasn't there.
        if FETCH_DETAILS and not job.get("categories"):
            enrich_job(job)
        hits = match_keywords(job, keywords) if keywords else ["(no keyword filter)"]
        if not hits:
            log.info("Skipped (category %s not in keywords): %s",
                     "/".join(job.get("categories", [])) or "?", job["title"])
            return
        if is_excluded(job):
            log.info("Skipped (excluded keyword/client %s): %s", job.get("client"), job["title"])
            return
        # For jobs we will alert, open the job page once if the search page
        # didn't include applicants / contract status / post date / profile picture.
        if (FETCH_DETAILS and not job.get("_enriched")
                and not all(job.get(k) for k in ("applicants", "contracts", "posted", "avatar_url"))):
            enrich_job(job)
            if is_excluded(job):  # client name may only be known now
                log.info("Skipped (excluded keyword/client %s): %s", job.get("client"), job["title"])
                return
        pd = _fmt_date(job.get("posted"))
        job["is_new"] = bool(pd and (date.today() - pd).days <= 1)
        if dry_run:
            print(f"MATCH  {job['title']}  [{', '.join(hits)}]  {job['url']}")
        else:
            send_job(job, hits)
            log.info("Alerted: %s  [%s]", job["title"], ", ".join(hits))
            time.sleep(1)  # Telegram allows about 1 message per second per chat
    except Exception as e:  # one bad job must not stop the others
        log.error("Error handling job %s (%s): %s", job["id"], job.get("url"), e)


def check_once(dry_run=False):
    """Run one check over all search pages.

    Each search page is handled as soon as it is fetched: its new jobs are
    alerted immediately, before the next page is requested.

    The seen list is re-read from disk at the start of every check and each job
    is saved as seen BEFORE its alert is sent. So a job is never alerted twice,
    even after an error, a restart, or a Telegram timeout.
    """
    stored = None if dry_run else load_seen()
    first_run = stored is None and not dry_run
    seen_list = list(stored or [])
    seen = set(seen_list)
    keywords = load_keywords()
    total_new = 0

    for url in SEARCH_URLS:
        try:
            jobs = fetch_jobs(url)
        except requests.RequestException as e:
            log.error("Fetch failed for %s: %s", url, e)
            continue
        if not jobs:
            log.warning("No jobs parsed from %s (page layout may have changed)", url)
        new_jobs = []
        for job in jobs:
            if job["id"] not in seen:
                seen.add(job["id"])  # also dedupes a job listed under two URLs
                job["source"] = url
                new_jobs.append(job)
        log.info("Fetched %d jobs (%d new) from %s", len(jobs), len(new_jobs), url)
        total_new += len(new_jobs)

        if first_run and not ALERT_ON_FIRST_RUN:
            seen_list += [j["id"] for j in new_jobs]
            save_seen(seen_list)
            continue
        if not new_jobs:
            continue
        if SCREENSHOT and CARD_IMAGE and not dry_run:
            _apply_screenshots(new_jobs, url)
        # Search pages list newest first; send oldest first so chat order is natural.
        for job in reversed(new_jobs):
            _handle_job(job, keywords, seen_list, dry_run)

    if first_run and not ALERT_ON_FIRST_RUN:
        log.info("First run: marked %d existing jobs as seen (no alerts)", total_new)
        try:
            send_message(
                f"✅ CrowdWorks monitor started.\nWatching {len(SEARCH_URLS)} search page(s) "
                f"with {len(keywords)} keywords. {total_new} existing jobs skipped; "
                f"you'll get alerts for new matching posts."
            )
        except Exception as e:
            log.error("Telegram send failed: %s", e)


def get_chat_id():
    updates = tg_api("getUpdates")
    if not updates:
        print("No messages found. Send any message to your bot in Telegram, then run this again.")
        return
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


def main():
    ap = argparse.ArgumentParser(description="CrowdWorks -> Telegram new job alerts")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--get-chat-id", action="store_true", help="show chat IDs that messaged the bot")
    ap.add_argument("--debug", action="store_true",
                    help="save the first job's raw data from each URL to debug_job.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="check current listings, print matches, send nothing, save nothing")
    args = ap.parse_args()

    if args.debug:
        out = {}
        for url in SEARCH_URLS[:1]:
            page = polite_get(url)
            jobs = parse_jobs(page)
            out = {"url": url, "parsed_first_job": {k: str(v) for k, v in (jobs[0] if jobs else {}).items()},
                   "raw_first_job": _DEBUG_ITEM, "html_length": len(page)}
        path = BASE_DIR / "debug_job.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Saved {path}. Please send this file.")
        return
    if args.dry_run:
        check_once(dry_run=True)
        return
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (copy env.example to .env)")
    if args.get_chat_id:
        get_chat_id()
        return
    if not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID is not set. Run with --get-chat-id to find it.")
    if args.test:
        send_message("👋 Test message from your CrowdWorks alert bot.")
        print("Sent.")
        return

    acquire_single_instance_lock()
    log.info("Seen-jobs file: %s", SEEN_FILE)
    if args.once:
        check_once()
        return

    log.info("Monitoring %d URL(s) every %ds", len(SEARCH_URLS), CHECK_INTERVAL)
    while True:
        try:
            check_once()
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
