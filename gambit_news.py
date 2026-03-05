#!/usr/bin/env python3
"""
gambit_news.py – Schacknyhetssystem för gambit.se
RSS-first, Claude-översättning, WordPress-publicering

Kommandon:
  python gambit_news.py --collect     Hämta och översätt nya artiklar
  python gambit_news.py --approve     Öppna godkännandesidan i webbläsaren
  python gambit_news.py --publish     Publicera godkända artiklar på WordPress
  python gambit_news.py --status      Visa statistik
  python gambit_news.py --test        Testa alla RSS-flöden
"""

import os
import sys
import json
import time
import logging
import argparse
import hashlib
import webbrowser
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ── Miljövariabler ──────────────────────────────────────────────────────────
load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
WP_URL            = os.getenv("WP_URL", "").rstrip("/")
WP_USER           = os.getenv("WP_USER", "Gambit")
WP_PASS           = os.getenv("WP_PASS", "")
GAMBIT_TOKEN      = os.getenv("GAMBIT_TOKEN", "")
EMAIL_FROM        = os.getenv("EMAIL_FROM", "")
EMAIL_TO          = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD", "")
SMTP_SERVER       = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT", "587"))

# ── Filer ───────────────────────────────────────────────────────────────────
SEEN_FILE      = Path("seen_articles.json")
PENDING_FILE   = Path("pending_approval.json")
DECISIONS_FILE = Path("article_decisions.json")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("gambit_news.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Claude-klient ────────────────────────────────────────────────────────────
claude = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic
        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        log.info("✅ Claude API redo")
    except ImportError:
        log.error("❌ pip install anthropic saknas")
else:
    log.warning("⚠️  ANTHROPIC_API_KEY saknas i .env")


# ════════════════════════════════════════════════════════════════════════════
# KÄLLOR
# ════════════════════════════════════════════════════════════════════════════

SOURCES = [
    {
        "name": "Chess.com",
        "rss":  "https://www.chess.com/rss/news",
        "lang": "en",
        "wp_category": "chess-com",
        "max": 12,
    },
    {
        "name": "ChessBase",
        "rss":  "https://en.chessbase.com/feed",
        "lang": "en",
        "wp_category": "chessbase",
        "max": 8,
    },
    {
        "name": "FIDE",
        "rss":  "https://www.fide.com/feed",
        "lang": "en",
        "wp_category": "fide",
        "max": 8,
    },
    {
        "name": "Schack.se",
        "rss":  "https://schack.se/feed",
        "lang": "sv",
        "wp_category": "schack-se",
        "max": 10,
    },
    {
        "name": "ChessBase India",
        "rss":  "https://chessbase.in/rss",
        "lang": "en",
        "wp_category": "chessbase-india",
        "max": 8,
    },
    {
        "name": "Chessdom",
        "rss":  "https://www.chessdom.com/feed",
        "lang": "en",
        "wp_category": "chessdom",
        "max": 6,
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ════════════════════════════════════════════════════════════════════════════
# HJÄLPFUNKTIONER
# ════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def url_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:10]


def fetch(url, timeout=15):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
        except Exception as e:
            if attempt == 2:
                log.debug(f"  fetch misslyckades {url}: {e}")
            time.sleep(3)
    return None


def parse_rss_date(date_str):
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        pass
    try:
        from dateutil import parser as dp
        return dp.parse(date_str).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def strip_html(html):
    text = BeautifulSoup(html or "", "html.parser").get_text(separator=" ")
    return " ".join(text.split())


# ════════════════════════════════════════════════════════════════════════════
# RSS-INSAMLING
# ════════════════════════════════════════════════════════════════════════════

def fetch_rss(source):
    log.info(f"  📡 {source['name']} – hämtar RSS...")
    r = fetch(source["rss"])
    if not r:
        log.warning(f"  ⚠️  {source['name']}: kunde inte hämta RSS")
        return []

    articles = []
    try:
        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[: source["max"]]:
            title_el = item.find("title")
            title = strip_html(title_el.text if title_el is not None else "")
            if not title or len(title) < 10:
                continue

            link_el = item.find("link")
            if link_el is None:
                link_el = item.find("atom:link", ns)
            url = ""
            if link_el is not None:
                url = (link_el.text or link_el.get("href", "")).strip()
            if not url:
                continue

            date_el = item.find("pubDate") or item.find("dc:date") or item.find("atom:updated", ns)
            date_str = parse_rss_date(date_el.text if date_el is not None else "")

            desc_el = item.find("description") or item.find("atom:summary", ns) or item.find("atom:content", ns)
            excerpt = strip_html(desc_el.text if desc_el is not None else "")[:500]

            articles.append({
                "id":      url_id(url),
                "source":  source["name"],
                "lang":    source["lang"],
                "wp_cat":  source["wp_category"],
                "url":     url,
                "title":   title,
                "excerpt": excerpt,
                "date":    date_str,
            })

    except ET.ParseError as e:
        log.warning(f"  ⚠️  {source['name']}: XML-fel: {e}")
        return []

    log.info(f"  ✅ {source['name']}: {len(articles)} artiklar")
    return articles


def fetch_article_body(url):
    r = fetch(url)
    if not r:
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    for tag in soup(["nav", "footer", "script", "style", "aside", "header"]):
        tag.decompose()

    selectors = [
        "article", ".article-body", ".post-content", ".entry-content",
        ".news-content", ".content-body", "main", ".article-content",
        ".post-body", "[itemprop='articleBody']"
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator=" ")
            text = " ".join(text.split())
            if len(text) > 200:
                return text[:3000]

    body = soup.find("body")
    if body:
        text = " ".join(body.get_text(separator=" ").split())
        return text[:3000]

    return ""


# ════════════════════════════════════════════════════════════════════════════
# CLAUDE-ÖVERSÄTTNING
# ════════════════════════════════════════════════════════════════════════════

LANG_NAMES = {"en": "engelska", "fr": "franska", "sv": "svenska"}

TRANSLATE_PROMPT = """\
Du är redaktör på den svenska schackportalen Gambit.se och skriver korta, snappy nyhetsnotiser.

UPPGIFT:
Skriv en svensk nyhetsnotis baserad på källmaterialet nedan.

REGLER:
- Rubrik: max 10 ord, engagerande, på svenska
- Text: 80-150 ord, objektiv nyhetsprosa, svenska schacktermer
- Nämn källan naturligt en gång i texten (t.ex. "enligt Chess.com", "rapporterar ChessBase")
- Behåll spelarnamn, turneringsnamn och förkortningar exakt som i originalet
- Inga bildtexter, inga hänvisningar till foton eller diagram
- Avsluta INTE med "Läs mer"-länk

KÄLLA: {source} ({lang})
ORIGINALTITEL: {title}
ORIGINALTEXT:
{body}

Svara ENBART i detta format:
RUBRIK: [din rubrik]
TEXT: [din notis]"""


def translate(article):
    if not claude:
        log.warning("  ⚠️  Claude inte tillgänglig")
        return None

    lang_name = LANG_NAMES.get(article["lang"], article["lang"])

    body = article["excerpt"]
    if len(body) < 150:
        log.info(f"    🔍 Hämtar artikeltext...")
        fetched = fetch_article_body(article["url"])
        if fetched:
            body = fetched
        time.sleep(2)

    if not body:
        log.warning(f"  ⚠️  Tomt innehåll för {article['url']}")
        return None

    prompt = TRANSLATE_PROMPT.format(
        source=article["source"],
        lang=lang_name,
        title=article["title"],
        body=body[:2500],
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        if "RUBRIK:" in raw and "TEXT:" in raw:
            parts = raw.split("TEXT:", 1)
            sv_title = parts[0].replace("RUBRIK:", "").strip()
            sv_text  = parts[1].strip()
        else:
            lines = raw.splitlines()
            sv_title = lines[0].strip()
            sv_text  = "\n".join(lines[1:]).strip()

        if not sv_title or not sv_text:
            log.warning(f"  ⚠️  Tomt Claude-svar för {article['title']}")
            return None

        result = {**article, "sv_title": sv_title, "sv_text": sv_text}
        log.info(f"  ✅ Översatt: {sv_title}")
        return result

    except Exception as e:
        log.error(f"  ❌ Claude-fel: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# SKICKA TILL WORDPRESS
# ════════════════════════════════════════════════════════════════════════════

def get_seen_from_wordpress():
    """Hämta lista på sedda URL-hash:ar från WordPress."""
    if not all([WP_URL, GAMBIT_TOKEN]):
        return set()
    headers = {"X-Gambit-Token": GAMBIT_TOKEN}
    try:
        r = requests.get(f"{WP_URL}/wp-json/gambit/v1/seen", headers=headers, timeout=15)
        if r.ok:
            data = r.json()
            seen = set(data.get("seen", {}).keys())
            log.info(f"📋 {len(seen)} sedda artiklar i WordPress")
            return seen
    except Exception as e:
        log.warning(f"⚠️  Kunde inte hämta sedda URL:er: {e}")
    return set()


def send_to_wordpress(articles):
    if not all([WP_URL, GAMBIT_TOKEN]):
        log.error("❌ WP_URL eller GAMBIT_TOKEN saknas")
        return 0

    url = f"{WP_URL}/wp-json/gambit/v1/ingest"
    headers = {
        "Content-Type": "application/json",
        "X-Gambit-Token": GAMBIT_TOKEN,
    }

    try:
        r = requests.post(url, json=articles, headers=headers, timeout=30)
        if r.ok:
            data = r.json()
            saved = data.get("saved", 0)
            log.info(f"✅ Skickade {saved} artiklar till WordPress")
            return saved
        else:
            log.error(f"❌ WordPress svarade {r.status_code}: {r.text[:200]}")
            return 0
    except Exception as e:
        log.error(f"❌ Kunde inte nå WordPress: {e}")
        return 0


# ════════════════════════════════════════════════════════════════════════════
# E-POSTNOTIFIERING
# ════════════════════════════════════════════════════════════════════════════

def send_notification_email(count):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.info("📧 E-postinställningar saknas – hoppar över notifiering")
        return

    body = f"""Hej!

{count} nya schackartiklar har översatts och väntar på din granskning.

Gå till redaktionssidan:
{WP_URL}/wp-admin/admin.php?page=gambit-redaktion

/Gambit News
"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg["Subject"] = f"schack {count} nya artiklar vantar pa granskning"

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        log.info(f"📧 E-post skickat till {EMAIL_TO}")
    except Exception as e:
        log.warning(f"⚠️  Kunde inte skicka e-post: {e}")


# ════════════════════════════════════════════════════════════════════════════
# WORDPRESS-PUBLICERING (manuell väg)
# ════════════════════════════════════════════════════════════════════════════

def wp_ensure_category(name, slug):
    api  = f"{WP_URL}/wp-json/wp/v2/categories"
    auth = (WP_USER, WP_PASS)

    r = requests.get(api, params={"slug": slug}, auth=auth, timeout=10)
    if r.ok:
        cats = r.json()
        if cats:
            return cats[0]["id"]

    r = requests.post(api, json={"name": name, "slug": slug}, auth=auth, timeout=10)
    if r.ok:
        return r.json()["id"]

    return 1


def wp_publish(article):
    if not all([WP_URL, WP_USER, WP_PASS]):
        log.error("❌ WordPress-inställningar saknas")
        return None

    cat_id = wp_ensure_category(article["source"], article["wp_cat"])
    date_display = article["date"][:10] if article.get("date") else "okänt datum"

    content = f"""{article['sv_text']}

<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">
<p style="font-size:0.85em;color:#777;font-style:italic;">
  Källa: <a href="{article['url']}" target="_blank" rel="noopener">{article['source']}</a> &nbsp;·&nbsp;
  Publicerad: {date_display} &nbsp;·&nbsp;
  Bearbetad med AI
</p>"""

    post = {
        "title":      article["sv_title"],
        "content":    content,
        "excerpt":    article["sv_text"][:160],
        "status":     "publish",
        "categories": [cat_id],
        "meta": {
            "source_url":  article["url"],
            "source_name": article["source"],
        },
    }

    if article.get("date"):
        try:
            from dateutil import parser as dp
            dt = dp.parse(article["date"])
            wp_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
            post["date"]     = wp_date
            post["date_gmt"] = wp_date
        except Exception:
            pass

    auth = (WP_USER, WP_PASS)
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json=post,
        auth=auth,
        timeout=30,
    )

    if r.status_code == 201:
        url = r.json().get("link", "")
        log.info(f"  ✅ Publicerad: {article['sv_title']}")
        log.info(f"     {url}")
        return url
    else:
        log.error(f"  ❌ WordPress-fel {r.status_code}: {r.text[:200]}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# GODKÄNNANDESIDA (lokal HTML)
# ════════════════════════════════════════════════════════════════════════════

APPROVE_HTML = """\
<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<title>Gambit.se – Granska artiklar</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #f4f4f4; color: #222; padding: 20px; }}
  .toolbar {{
    position: sticky; top: 0; background: #1a1a2e; color: #fff;
    padding: 12px 20px; border-radius: 6px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 16px; z-index: 10;
  }}
  .toolbar h1 {{ color: #fff; font-size: 1.1rem; margin: 0; flex: 1; }}
  .counter {{ font-size: .82rem; color: #aaa; }}
  .save-btn {{
    background: #e91e63; color: #fff; border: none; border-radius: 5px;
    padding: 8px 20px; font-size: .9rem; font-weight: 700; cursor: pointer;
  }}
  .all-btn {{
    background: #333; color: #ccc; border: none; border-radius: 4px;
    padding: 5px 12px; font-size: .75rem; cursor: pointer;
  }}
  .card {{
    background: #fff; border: 1px solid #ddd; border-radius: 8px;
    padding: 18px 20px; margin-bottom: 16px;
    transition: border-color .15s, opacity .2s;
  }}
  .card.publish {{ border-left: 4px solid #2e7d32; }}
  .card.skip    {{ border-left: 4px solid #c62828; opacity: .6; }}
  .source-tag {{
    display: inline-block; font-size: .68rem; font-weight: 700;
    letter-spacing: .08em; text-transform: uppercase;
    padding: 2px 8px; border-radius: 3px; color: #fff; margin-bottom: 10px;
  }}
  .orig-title {{ font-size: .8rem; color: #888; margin-bottom: 6px; }}
  .title-input {{
    width: 100%; font-size: 1.05rem; font-weight: 700;
    border: 1px solid #ddd; border-radius: 4px; padding: 6px 10px;
    margin-bottom: 10px; font-family: inherit;
  }}
  .text-input {{
    width: 100%; min-height: 100px; font-size: .88rem; line-height: 1.6;
    border: 1px solid #ddd; border-radius: 4px; padding: 8px 10px;
    font-family: inherit; resize: vertical;
  }}
  .meta {{ font-size: .72rem; color: #999; margin-top: 8px; }}
  .meta a {{ color: #1565c0; }}
  .actions {{ display: flex; gap: 8px; margin-top: 12px; }}
  .btn {{ padding: 6px 16px; border: none; border-radius: 4px; font-size: .82rem; font-weight: 600; cursor: pointer; }}
  .btn-pub  {{ background: #2e7d32; color: #fff; }}
  .btn-skip {{ background: #c62828; color: #fff; }}
  #save-status {{ font-size: .8rem; color: #aaa; }}
</style>
</head>
<body>
<div class="toolbar">
  <h1>♟ Gambit.se – Artikelgranskning</h1>
  <span class="counter" id="counter"></span>
  <button class="all-btn" onclick="setAll('publish')">Publicera alla</button>
  <button class="all-btn" onclick="setAll('skip')">Hoppa över alla</button>
  <button class="save-btn" onclick="saveDecisions()">Spara beslut</button>
  <span id="save-status"></span>
</div>
<div id="articles"></div>
<script>
const COLORS = {{"Chess.com":"#388e3c","ChessBase":"#e65100","FIDE":"#1565c0","Schack.se":"#6a1b9a","Europe Echecs":"#795548"}};
const articles = {articles_json};
const decisions = {{}};

function esc(s) {{ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }}

function renderAll() {{
  document.getElementById("articles").innerHTML = articles.map(a => `
    <div class="card" id="card-${{a.id}}">
      <span class="source-tag" style="background:${{COLORS[a.source]||'#555'}}">${{a.source}}</span>
      <div class="orig-title">Original: ${{esc(a.title)}}</div>
      <input class="title-input" id="title-${{a.id}}" value="${{esc(a.sv_title)}}">
      <textarea class="text-input" id="text-${{a.id}}">${{esc(a.sv_text)}}</textarea>
      <div class="meta">${{a.date ? a.date.slice(0,10) : ""}} &nbsp;·&nbsp; <a href="${{a.url}}" target="_blank">Originalartikeln</a></div>
      <div class="actions">
        <button class="btn btn-pub"  onclick="decide('${{a.id}}','publish')">Publicera</button>
        <button class="btn btn-skip" onclick="decide('${{a.id}}','skip')">Hoppa over</button>
      </div>
    </div>`).join("");
  updateCounter();
}}

function decide(id, action) {{
  decisions[id] = action;
  document.getElementById("card-"+id).className = "card "+action;
  updateCounter();
}}

function setAll(action) {{ articles.forEach(a => decide(a.id, action)); }}

function updateCounter() {{
  const pub  = Object.values(decisions).filter(v => v==="publish").length;
  const skip = Object.values(decisions).filter(v => v==="skip").length;
  document.getElementById("counter").textContent = articles.length+" artiklar  Publicera: "+pub+"  Hoppa over: "+skip;
}}

function saveDecisions() {{
  const result = articles.map(a => ({{
    ...a,
    sv_title: document.getElementById("title-"+a.id)?.value || a.sv_title,
    sv_text:  document.getElementById("text-"+a.id)?.value  || a.sv_text,
    decision: decisions[a.id] || "pending",
  }}));
  const blob = new Blob([JSON.stringify(result, null, 2)], {{type:"application/json"}});
  const url  = URL.createObjectURL(blob);
  const el   = document.createElement("a");
  el.href = url; el.download = "pending_approval.json"; el.click();
  URL.revokeObjectURL(url);
  document.getElementById("save-status").textContent = "Nedladdad! Lagg filen i samma mapp och kor --publish";
  setTimeout(() => document.getElementById("save-status").textContent = "", 8000);
}}

renderAll();
</script>
</body>
</html>
"""


def generate_approve_page(articles):
    html = APPROVE_HTML.replace("{articles_json}", json.dumps(articles, ensure_ascii=False))
    path = Path("gambit_approve.html")
    path.write_text(html, encoding="utf-8")
    return path


# ════════════════════════════════════════════════════════════════════════════
# BESLUTS-LOGGNING
# ════════════════════════════════════════════════════════════════════════════

def log_decisions(articles):
    existing = load_json(DECISIONS_FILE, [])
    for a in articles:
        existing.append({
            "timestamp": datetime.now().isoformat(),
            "id":        a["id"],
            "source":    a["source"],
            "url":       a["url"],
            "title":     a.get("sv_title", a.get("title")),
            "decision":  a.get("decision", "pending"),
            "wp_url":    a.get("wp_url"),
        })
    save_json(DECISIONS_FILE, existing)


# ════════════════════════════════════════════════════════════════════════════
# KOMMANDON
# ════════════════════════════════════════════════════════════════════════════

def cmd_collect():
    log.info("=" * 60)
    log.info("Startar insamling")

    # Hämta sedda URL:er från WordPress (primär källa)
    wp_seen_hashes = get_seen_from_wordpress()

    # Lokal backup
    seen = set(load_json(SEEN_FILE, []))

    pending = load_json(PENDING_FILE, [])
    pending_ids = {a["id"] for a in pending}

    new_raw = []
    for source in SOURCES:
        articles = fetch_rss(source)
        for a in articles:
            url_hash = __import__('hashlib').md5(a['url'].encode()).hexdigest()
            if a["id"] not in seen and a["id"] not in pending_ids and url_hash not in wp_seen_hashes:
                new_raw.append(a)
        time.sleep(1)

    log.info(f"{len(new_raw)} nya artiklar att oversatta")

    if not new_raw:
        log.info("Inga nya artiklar")
        return

    if not claude:
        log.error("Claude inte tillganglig")
        return

    translated = []
    for i, a in enumerate(new_raw, 1):
        log.info(f"  [{i}/{len(new_raw)}] {a['source']}: {a['title'][:60]}")
        result = translate(a)
        if result:
            translated.append(result)
        seen.add(a["id"])
        time.sleep(1.5)

    save_json(SEEN_FILE, list(seen))

    if translated:
        pending.extend(translated)
        save_json(PENDING_FILE, pending)
        log.info(f"{len(translated)} artiklar sparade")

        saved = send_to_wordpress(translated)
        if saved > 0:
            send_notification_email(saved)
        else:
            send_notification_email(len(translated))
    else:
        log.warning("Inga artiklar oversattes")


def cmd_approve():
    pending = load_json(PENDING_FILE, [])
    to_review = [a for a in pending if a.get("decision", "pending") == "pending"]

    if not to_review:
        log.info("Inga artiklar att granska")
        return

    path = generate_approve_page(to_review)
    log.info(f"Oppnar {path.absolute()} i webbläsaren")
    webbrowser.open(f"file://{path.absolute()}")


def cmd_publish():
    pending = load_json(PENDING_FILE, [])
    to_publish = [a for a in pending if a.get("decision") == "publish"]

    if not to_publish:
        log.info("Inga artiklar markerade for publicering")
        return

    log.info(f"Publicerar {len(to_publish)} artiklar...")

    skipped_ids = {a["id"] for a in pending if a.get("decision") == "skip"}

    for a in to_publish:
        wp_url = wp_publish(a)
        a["wp_url"] = wp_url
        a["published_at"] = datetime.now().isoformat()
        time.sleep(1)

    log_decisions(pending)

    remaining = [
        a for a in pending
        if a.get("decision", "pending") == "pending"
        and a["id"] not in skipped_ids
    ]
    save_json(PENDING_FILE, remaining)

    ok = sum(1 for a in to_publish if a.get("wp_url"))
    log.info(f"Publicerade: {ok}/{len(to_publish)}")


def cmd_status():
    decisions = load_json(DECISIONS_FILE, [])
    pending   = load_json(PENDING_FILE, [])
    seen      = load_json(SEEN_FILE, [])

    pub  = [d for d in decisions if d.get("decision") == "publish"]
    skip = [d for d in decisions if d.get("decision") == "skip"]
    wait = [a for a in pending  if a.get("decision", "pending") == "pending"]

    print("\n" + "=" * 50)
    print("  GAMBIT NEWS - STATISTIK")
    print("=" * 50)
    print(f"  Sedda artiklar totalt:  {len(seen)}")
    print(f"  Vantar pa granskning:   {len(wait)}")
    print(f"  Publicerade (totalt):   {len(pub)}")
    print(f"  Overhoppade (totalt):   {len(skip)}")
    print("=" * 50 + "\n")


def cmd_test():
    log.info("Testar RSS-floden...")
    for source in SOURCES:
        articles = fetch_rss(source)
        if articles:
            log.info(f"  OK {source['name']:20s} {len(articles)} artiklar")
            log.info(f"     Senaste: {articles[0]['title'][:70]}")
        else:
            log.warning(f"  FEL {source['name']:20s}")
    log.info("Klar.")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Gambit.se schacknyhetssystem")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--status",  action="store_true")
    parser.add_argument("--test",    action="store_true")

    args = parser.parse_args()

    if args.collect:
        cmd_collect()
    elif args.approve:
        cmd_approve()
    elif args.publish:
        cmd_publish()
    elif args.status:
        cmd_status()
    elif args.test:
        cmd_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
