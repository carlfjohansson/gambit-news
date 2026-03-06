#!/usr/bin/env python3
"""
gambit_news.py – Schacknyhetssystem för gambit.se
"""

import os, sys, json, time, logging, argparse, hashlib, webbrowser, smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

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

SEEN_FILE      = Path("seen_articles.json")
PENDING_FILE   = Path("pending_approval.json")
DECISIONS_FILE = Path("article_decisions.json")

SUBJECT_CATEGORIES = []  # fylls dynamiskt från WordPress
CATEGORY_SLUGS = {}      # fylls dynamiskt från WordPress

def load_wp_categories():
    """Hämta kategorier från WordPress REST API."""
    global SUBJECT_CATEGORIES, CATEGORY_SLUGS
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"per_page": 100, "hide_empty": False},
            auth=(WP_USER, WP_PASS),
            timeout=10
        )
        if r.status_code == 200:
            skip_slugs = {"uncategorized", "okategoriserade", "okategoriserad-en"}
            cats = [c for c in r.json() if c["slug"] not in skip_slugs]
            SUBJECT_CATEGORIES = [c["name"] for c in cats]
            CATEGORY_SLUGS = {c["name"]: c["slug"] for c in cats}
            log.info(f"✅ Kategorier laddade: {', '.join(SUBJECT_CATEGORIES)}")
        else:
            log.warning(f"⚠️  Kunde inte hämta kategorier ({r.status_code}), använder fallback")
            _set_fallback_categories()
    except Exception as e:
        log.warning(f"⚠️  Kategorifel: {e}, använder fallback")
        _set_fallback_categories()

def _set_fallback_categories():
    global SUBJECT_CATEGORIES, CATEGORY_SLUGS
    SUBJECT_CATEGORIES = ["Internationellt"]
    CATEGORY_SLUGS = {"Internationellt": "internationellt"}

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

claude = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic
        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        log.info("✅ Claude API redo")
    except ImportError:
        log.error("❌ pip install anthropic saknas")

SOURCES = [
    {"name": "Chess.com",       "rss": "https://www.chess.com/rss/news",                  "lang": "en", "max": 12},
    {"name": "ChessBase",       "rss": "https://en.chessbase.com/feed",                   "lang": "en", "max": 8},
    {"name": "FIDE",            "rss": "https://www.fide.com/feed",                       "lang": "en", "max": 8},
    {"name": "Schack.se",       "rss": "https://schack.se/feed",                          "lang": "sv", "max": 10},
    {"name": "ChessBase India", "rss": "https://chessbase.in/rss",                        "lang": "en", "max": 8},
    {"name": "Chessdom",        "rss": "https://www.chessdom.com/feed",                   "lang": "en", "max": 6},
    {"name": "Bergensjakk",     "rss": "https://bergensjakk.no/feed",                     "lang": "no", "max": 6},
    {"name": "TWIC",            "rss": "https://theweekinchess.com/twic-rss-feed",        "lang": "en", "max": 6},
    {"name": "US Chess",        "rss": "https://new.uschess.org/feed.xml",                "lang": "en", "max": 6},
    {"name": "Kingpin Chess",   "rss": "https://www.kingpinchess.net/feed/",              "lang": "en", "max": 4},
    {"name": "ChessBox India",  "rss": "https://www.chessbox.in/feed/",                   "lang": "en", "max": 6},
    {"name": "Africa Chess",    "rss": "https://africachess.net/feed/",                   "lang": "en", "max": 4},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
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
    """Parsar källans pubDate. Returnerar ISO-sträng eller None."""
    if not date_str:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).isoformat()
    except:
        pass
    try:
        from dateutil import parser as dp
        return dp.parse(date_str).isoformat()
    except:
        return None

def strip_html(html):
    text = BeautifulSoup(html or "", "html.parser").get_text(separator=" ")
    return " ".join(text.split())

def fetch_rss(source):
    log.info(f"  📡 {source['name']} – hämtar RSS...")
    r = fetch(source["rss"])
    if not r:
        log.warning(f"  ⚠️  {source['name']}: kunde inte hämta RSS")
        return []

    articles = []
    try:
        # Använd BeautifulSoup med xml-parser – hanterar namespaces automatiskt
        soup = BeautifulSoup(r.content, "xml")
        items = soup.find_all("item") or soup.find_all("entry")

        for item in items[:source["max"]]:
            # Titel
            title_el = item.find("title")
            title = strip_html(title_el.get_text() if title_el else "")
            if not title or len(title) < 10:
                continue

            # URL
            url = ""
            link_el = item.find("link")
            if link_el:
                url = (link_el.get_text().strip() or link_el.get("href", "")).strip()
            if not url:
                continue

            # Datum från källan
            date_el = (item.find("pubDate") or item.find("published") or
                       item.find("updated") or item.find("date"))
            pub_date = parse_rss_date(date_el.get_text() if date_el else "")

            # Ingress
            desc_el = item.find("description") or item.find("summary") or item.find("content")
            excerpt = strip_html(desc_el.get_text() if desc_el else "")[:500]

            articles.append({
                "id":      url_id(url),
                "source":  source["name"],
                "lang":    source["lang"],
                "url":     url,
                "title":   title,
                "excerpt": excerpt,
                "date":    pub_date,
            })

    except Exception as e:
        log.warning(f"  ⚠️  {source['name']}: parse-fel: {e}")
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
    for sel in ["article", ".article-body", ".post-content", ".entry-content",
                ".news-content", ".content-body", "main", ".article-content"]:
        el = soup.select_one(sel)
        if el:
            text = " ".join(el.get_text(separator=" ").split())
            if len(text) > 200:
                return text[:3000]
    body = soup.find("body")
    if body:
        return " ".join(body.get_text(separator=" ").split())[:3000]
    return ""

LANG_NAMES = {"en": "engelska", "fr": "franska", "sv": "svenska", "no": "norska"}

TRANSLATE_PROMPT = """\
Du är redaktör på den svenska schackportalen Gambit.se och skriver korta, snappy nyhetsnotiser.

UPPGIFT:
Skriv en svensk nyhetsnotis baserad på källmaterialet nedan. Välj också rätt ämneskategori.

REGLER FÖR NOTISEN:
- Rubrik: max 10 ord, engagerande, på svenska
- Text: 80-150 ord, objektiv nyhetsprosa, svenska schacktermer
- Nämn källan naturligt en gång i texten (t.ex. "enligt Chess.com", "rapporterar ChessBase")
- Behåll spelarnamn, turneringsnamn och förkortningar exakt som i originalet
- Inga bildtexter, inga hänvisningar till foton eller diagram
- Avsluta INTE med "Läs mer"-länk

KATEGORIER (välj EN som passar bäst):
- Turneringar & resultat       (tävlingsresultat, pågående turneringar, rundrapporter)
- Toppschack & elitspelare     (nyheter om GM/IM, ratings, intervjuer, karriär)
- Schackhistoria               (historiska partier, legender, retrospektiv)
- Schackpedagogik              (lärande, taktik, öppningar, träning)
- Svenska schacknyheter        (nyheter specifikt om svensk schack, svenska spelare/klubbar)
- Regler & FIDE                (FIDE-beslut, regeländringar, organisation)
- Internationellt              (nyheter från specifika länder/regioner som inte passar ovan)

KÄLLA: {source} ({lang})
ORIGINALTITEL: {title}
ORIGINALTEXT:
{body}

Svara ENBART i detta format:
RUBRIK: [din rubrik]
KATEGORI: [en av de sju kategorierna exakt som ovan]
TEXT: [din notis]"""

def translate(article):
    if not claude:
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
        return None

    prompt = TRANSLATE_PROMPT.format(
        source=article["source"], lang=lang_name,
        title=article["title"], body=body[:2500],
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=700, temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        sv_title = sv_category = sv_text = ""

        if "RUBRIK:" in raw:
            sv_title = raw.split("RUBRIK:", 1)[1].split("\n")[0].strip()
        if "KATEGORI:" in raw:
            kat = raw.split("KATEGORI:", 1)[1].split("\n")[0].strip()
            sv_category = kat if kat in SUBJECT_CATEGORIES else "Internationellt"
        if "TEXT:" in raw:
            sv_text = raw.split("TEXT:", 1)[1].strip()

        if not sv_title or not sv_text:
            lines = raw.splitlines()
            sv_title = lines[0].strip()
            sv_text  = "\n".join(lines[1:]).strip()

        if not sv_category:
            sv_category = "Internationellt"

        result = {
            **article,
            "sv_title":    sv_title,
            "sv_text":     sv_text,
            "sv_category": sv_category,
            "wp_cat":      CATEGORY_SLUGS.get(sv_category, "internationellt"),
        }
        log.info(f"  ✅ Översatt: {sv_title}")
        log.info(f"     Kategori: {sv_category}")
        return result

    except Exception as e:
        log.error(f"  ❌ Claude-fel: {e}")
        return None

def get_seen_from_wordpress():
    if not all([WP_URL, GAMBIT_TOKEN]):
        return set()
    try:
        r = requests.get(f"{WP_URL}/wp-json/gambit/v1/seen",
                         headers={"X-Gambit-Token": GAMBIT_TOKEN}, timeout=15)
        if r.ok:
            seen = set(r.json().get("seen", {}).keys())
            log.info(f"📋 {len(seen)} sedda artiklar i WordPress")
            return seen
    except Exception as e:
        log.warning(f"⚠️  Kunde inte hämta sedda URL:er: {e}")
    return set()

def send_to_wordpress(articles):
    if not all([WP_URL, GAMBIT_TOKEN]):
        log.error("❌ WP_URL eller GAMBIT_TOKEN saknas")
        return 0
    try:
        r = requests.post(
            f"{WP_URL}/wp-json/gambit/v1/ingest",
            json=articles,
            headers={"Content-Type": "application/json", "X-Gambit-Token": GAMBIT_TOKEN},
            timeout=30,
        )
        if r.ok:
            saved = r.json().get("saved", 0)
            log.info(f"✅ Skickade {saved} artiklar till WordPress")
            return saved
        log.error(f"❌ WordPress svarade {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"❌ Kunde inte nå WordPress: {e}")
    return 0

def send_notification_email(count):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        return
    msg = MIMEText(f"Hej!\n\n{count} nya schackartiklar väntar på granskning.\n\n{WP_URL}/redaktion\n\n/Gambit News", "plain", "utf-8")
    msg["From"] = EMAIL_FROM
    msg["To"]   = EMAIL_TO
    msg["Subject"] = f"Gambit: {count} nya artiklar väntar på granskning"
    try:
        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        s.starttls(); s.login(EMAIL_FROM, EMAIL_PASSWORD)
        s.send_message(msg); s.quit()
        log.info(f"📧 E-post skickat till {EMAIL_TO}")
    except Exception as e:
        log.warning(f"⚠️  Kunde inte skicka e-post: {e}")

def wp_ensure_category(name, slug):
    api  = f"{WP_URL}/wp-json/wp/v2/categories"
    auth = (WP_USER, WP_PASS)
    r = requests.get(api, params={"slug": slug}, auth=auth, timeout=10)
    if r.ok and r.json():
        return r.json()[0]["id"]
    r = requests.post(api, json={"name": name, "slug": slug}, auth=auth, timeout=10)
    return r.json()["id"] if r.ok else 1

def wp_publish(article):
    if not all([WP_URL, WP_USER, WP_PASS]):
        return None

    cat_name = article.get("sv_category", article["source"])
    cat_slug = article.get("wp_cat", "internationellt")
    cat_id   = wp_ensure_category(cat_name, cat_slug)

    date_display = article["date"][:10] if article.get("date") else ""
    content = (
        article['sv_text'] +
        "\n\n<hr style='margin:24px 0;border:none;border-top:1px solid #ddd;'>"
        f"<p style='font-size:0.85em;color:#777;font-style:italic;'>"
        f"Källa: <a href='{article['url']}' target='_blank' rel='noopener'>{article['source']}</a>"
        + (f" &nbsp;·&nbsp; {date_display}" if date_display else "")
        + " &nbsp;·&nbsp; Bearbetad med AI</p>"
    )

    post = {
        "title": article["sv_title"], "content": content,
        "excerpt": article["sv_text"][:160],
        "status": "publish", "categories": [cat_id],
        "meta": {"source_url": article["url"], "source_name": article["source"],
                 "sv_category": article.get("sv_category", "")},
    }

    # Källans publiceringsdatum
    if article.get("date"):
        try:
            from dateutil import parser as dp
            dt = dp.parse(article["date"])
            wp_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
            post["date"] = wp_date
            post["date_gmt"] = wp_date
        except:
            pass

    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=post,
                      auth=(WP_USER, WP_PASS), timeout=30)
    if r.status_code == 201:
        url = r.json().get("link", "")
        log.info(f"  ✅ Publicerad: {article['sv_title']}")
        return url
    log.error(f"  ❌ WordPress-fel {r.status_code}: {r.text[:200]}")
    return None

# ── Godkännandesida ──────────────────────────────────────────────────────────

APPROVE_HTML = """\
<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<title>GAMBIT – Granska artiklar</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #f4f4f4; color: #222; padding: 20px; }}
  .toolbar {{
    position: sticky; top: 0; background: #000; color: #fff;
    padding: 12px 20px; border-bottom: 3px solid #DEF947; margin-bottom: 20px;
    display: flex; align-items: center; gap: 16px; z-index: 10;
  }}
  .toolbar h1 {{ color: #DEF947; font-size: 1.1rem; margin: 0; flex: 1; letter-spacing: .1em; }}
  .counter {{ font-size: .82rem; color: #aaa; }}
  .save-btn {{ background: #DEF947; color: #000; border: none; border-radius: 3px; padding: 8px 20px; font-size: .9rem; font-weight: 700; cursor: pointer; }}
  .all-btn  {{ background: #333; color: #ccc; border: none; border-radius: 3px; padding: 5px 12px; font-size: .75rem; cursor: pointer; }}
  .card {{ background: #fff; border: 1px solid #ddd; border-radius: 4px; padding: 18px 20px; margin-bottom: 16px; border-left: 4px solid #ccc; }}
  .card.publish {{ border-left-color: #2F8AA6; background: #f4fafc; }}
  .card.skip    {{ border-left-color: #721121; opacity: .6; background: #fdf5f5; }}
  .source-tag {{ display: inline-block; font-size: .68rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; padding: 2px 8px; border-radius: 2px; color: #fff; margin-bottom: 10px; }}
  .orig-title {{ font-size: .8rem; color: #888; margin-bottom: 6px; }}
  .title-input {{ width: 100%; font-size: 1.05rem; font-weight: 700; border: 1px solid #ddd; border-radius: 3px; padding: 6px 10px; margin-bottom: 10px; font-family: inherit; }}
  .cat-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
  .cat-label {{ font-size: .72rem; color: #888; white-space: nowrap; }}
  .cat-select {{ flex: 1; font-size: .82rem; padding: 5px 8px; border: 1px solid #ddd; border-radius: 3px; font-family: inherit; background: #f9f9f9; }}
  .text-input {{ width: 100%; min-height: 100px; font-size: .88rem; line-height: 1.6; border: 1px solid #ddd; border-radius: 3px; padding: 8px 10px; font-family: inherit; resize: vertical; }}
  .meta {{ font-size: .72rem; color: #999; margin-top: 8px; }}
  .meta a {{ color: #2F8AA6; }}
  .actions {{ display: flex; gap: 8px; margin-top: 12px; }}
  .btn {{ padding: 6px 16px; border: none; border-radius: 3px; font-size: .82rem; font-weight: 700; cursor: pointer; }}
  .btn-pub  {{ background: #2F8AA6; color: #fff; }}
  .btn-skip {{ background: #721121; color: #fff; }}
  #save-status {{ font-size: .8rem; color: #aaa; }}
</style>
</head>
<body>
<div class="toolbar">
  <h1>GAMBIT – Artikelgranskning</h1>
  <span class="counter" id="counter"></span>
  <button class="all-btn" onclick="setAll('publish')">Publicera alla</button>
  <button class="all-btn" onclick="setAll('skip')">Hoppa över alla</button>
  <button class="save-btn" onclick="saveDecisions()">Spara beslut</button>
  <span id="save-status"></span>
</div>
<div id="articles"></div>
<script>
const SC = {{"Chess.com":"#2F8AA6","ChessBase":"#CF5C36","FIDE":"#1B4F5F","Schack.se":"#721121","Chessdom":"#2F8AA6","Bergensjakk":"#CF5C36","TWIC":"#333","US Chess":"#1B4F5F","Kingpin Chess":"#721121","ChessBase India":"#CF5C36","Africa Chess":"#2e7d32","ChessBox India":"#CF5C36"}};
const CATS = {categories_json};
const articles = {articles_json};
const decisions = {{}};

function esc(s) {{ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }}
function catOpts(sel) {{ return CATS.map(c=>`<option value="${{esc(c)}}" ${{c===sel?'selected':''}}>${{esc(c)}}</option>`).join(""); }}

function renderAll() {{
  document.getElementById("articles").innerHTML = articles.map(a=>`
    <div class="card" id="card-${{a.id}}">
      <span class="source-tag" style="background:${{SC[a.source]||'#555'}}">${{a.source}}</span>
      <div class="orig-title">Original: ${{esc(a.title)}}</div>
      <input class="title-input" id="title-${{a.id}}" value="${{esc(a.sv_title)}}">
      <div class="cat-row">
        <span class="cat-label">Kategori:</span>
        <select class="cat-select" id="cat-${{a.id}}">${{catOpts(a.sv_category||"")}}</select>
      </div>
      <textarea class="text-input" id="text-${{a.id}}">${{esc(a.sv_text)}}</textarea>
      <div class="meta">${{a.date?"📅 "+a.date.slice(0,10):"Datum saknas"}} &nbsp;·&nbsp; <a href="${{a.url}}" target="_blank">Originalartikeln →</a></div>
      <div class="actions">
        <button class="btn btn-pub"  onclick="decide('${{a.id}}','publish')">✅ Publicera</button>
        <button class="btn btn-skip" onclick="decide('${{a.id}}','skip')">✗ Hoppa över</button>
      </div>
    </div>`).join("");
  updateCounter();
}}

function decide(id,action) {{
  decisions[id]=action;
  document.getElementById("card-"+id).className="card "+action;
  updateCounter();
}}
function setAll(action) {{ articles.forEach(a=>decide(a.id,action)); }}
function updateCounter() {{
  const p=Object.values(decisions).filter(v=>v==="publish").length;
  const s=Object.values(decisions).filter(v=>v==="skip").length;
  document.getElementById("counter").textContent=articles.length+" artiklar  ·  ✅ "+p+"  ✗ "+s;
}}
function saveDecisions() {{
  const result=articles.map(a=>{{
    const cat=document.getElementById("cat-"+a.id)?.value||(a.sv_category||"Internationellt");
    return {{...a,
      sv_title:  document.getElementById("title-"+a.id)?.value||a.sv_title,
      sv_text:   document.getElementById("text-"+a.id)?.value||a.sv_text,
      sv_category: cat,
      wp_cat: cat.toLowerCase().replace(/[& ]+/g,"-").replace(/-+/g,"-").replace(/[åä]/g,"a").replace(/ö/g,"o"),
      decision: decisions[a.id]||"pending",
    }};
  }});
  const blob=new Blob([JSON.stringify(result,null,2)],{{type:"application/json"}});
  const url=URL.createObjectURL(blob);
  Object.assign(document.createElement("a"),{{href:url,download:"pending_approval.json"}}).click();
  URL.revokeObjectURL(url);
  document.getElementById("save-status").textContent="Nedladdad! Kör --publish";
  setTimeout(()=>document.getElementById("save-status").textContent="",8000);
}}
renderAll();
</script>
</body>
</html>
"""

def generate_approve_page(articles):
    html = APPROVE_HTML \
        .replace("{categories_json}", json.dumps(SUBJECT_CATEGORIES, ensure_ascii=False)) \
        .replace("{articles_json}",   json.dumps(articles, ensure_ascii=False))
    path = Path("gambit_approve.html")
    path.write_text(html, encoding="utf-8")
    return path

def log_decisions(articles):
    existing = load_json(DECISIONS_FILE, [])
    for a in articles:
        existing.append({
            "timestamp": datetime.now().isoformat(), "id": a["id"],
            "source": a["source"], "url": a["url"],
            "title": a.get("sv_title", a.get("title")),
            "category": a.get("sv_category", ""), "decision": a.get("decision", "pending"),
            "wp_url": a.get("wp_url"), "source_date": a.get("date"),
        })
    save_json(DECISIONS_FILE, existing)

def cmd_collect():
    log.info("=" * 60)
    log.info("Startar insamling")
    load_wp_categories()
    wp_seen = get_seen_from_wordpress()
    seen = set(load_json(SEEN_FILE, []))
    pending = load_json(PENDING_FILE, [])
    pending_ids = {a["id"] for a in pending}

    new_raw = []
    for source in SOURCES:
        for a in fetch_rss(source):
            url_hash = hashlib.md5(a['url'].encode()).hexdigest()
            if a["id"] not in seen and a["id"] not in pending_ids and url_hash not in wp_seen:
                new_raw.append(a)
        time.sleep(1)

    log.info(f"{len(new_raw)} nya artiklar att översätta")
    if not new_raw:
        return
    if not claude:
        log.error("Claude inte tillgänglig"); return

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
        saved = send_to_wordpress(translated)
        send_notification_email(saved if saved > 0 else len(translated))
    else:
        log.warning("Inga artiklar översattes")

def cmd_approve():
    pending = load_json(PENDING_FILE, [])
    to_review = [a for a in pending if a.get("decision", "pending") == "pending"]
    if not to_review:
        log.info("Inga artiklar att granska"); return
    path = generate_approve_page(to_review)
    webbrowser.open(f"file://{path.absolute()}")

def cmd_publish():
    pending = load_json(PENDING_FILE, [])
    to_publish = [a for a in pending if a.get("decision") == "publish"]
    if not to_publish:
        log.info("Inga artiklar markerade för publicering"); return
    skipped_ids = {a["id"] for a in pending if a.get("decision") == "skip"}
    for a in to_publish:
        a["wp_url"] = wp_publish(a)
        a["published_at"] = datetime.now().isoformat()
        time.sleep(1)
    log_decisions(pending)
    save_json(PENDING_FILE, [
        a for a in pending
        if a.get("decision", "pending") == "pending" and a["id"] not in skipped_ids
    ])
    ok = sum(1 for a in to_publish if a.get("wp_url"))
    log.info(f"Publicerade: {ok}/{len(to_publish)}")

def cmd_status():
    decisions = load_json(DECISIONS_FILE, [])
    pending   = load_json(PENDING_FILE, [])
    seen      = load_json(SEEN_FILE, [])
    pub  = [d for d in decisions if d.get("decision") == "publish"]
    skip = [d for d in decisions if d.get("decision") == "skip"]
    wait = [a for a in pending if a.get("decision", "pending") == "pending"]
    print(f"\n{'='*50}\n  GAMBIT NEWS - STATISTIK\n{'='*50}")
    print(f"  Sedda artiklar totalt:  {len(seen)}")
    print(f"  Väntar på granskning:   {len(wait)}")
    print(f"  Publicerade (totalt):   {len(pub)}")
    print(f"  Överhoppade (totalt):   {len(skip)}")
    if pub:
        from collections import Counter
        print("\n  Kategorifördelning:")
        for cat, count in Counter(d.get("category","?") for d in pub).most_common():
            print(f"    {cat}: {count}")
    print("="*50+"\n")

def cmd_test():
    log.info("Testar RSS-flöden...")
    for source in SOURCES:
        log.info(f"  📡 Testar {source['name']}...")
        r = fetch(source["rss"])
        if not r:
            log.warning(f"  FEL {source['name']:22s} – kunde inte hämta")
            continue

        # Visa råa bytes för felsökning
        preview = r.content[:300].decode('utf-8', errors='replace')
        log.info(f"  RAW: {repr(preview)}")

        articles = fetch_rss(source)
        if articles:
            a = articles[0]
            log.info(f"  OK  {source['name']:22s} {len(articles)} artiklar")
            log.info(f"      Senaste: {a['title'][:65]}")
            log.info(f"      Datum:   {a.get('date') or 'saknas'}")
        else:
            log.warning(f"  FEL {source['name']:22s} – 0 artiklar trots svar")
    log.info("Klar.")

def main():
    parser = argparse.ArgumentParser(description="Gambit.se schacknyhetssystem")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--status",  action="store_true")
    parser.add_argument("--test",    action="store_true")
    args = parser.parse_args()
    if   args.collect: cmd_collect()
    elif args.approve: cmd_approve()
    elif args.publish: cmd_publish()
    elif args.status:  cmd_status()
    elif args.test:    cmd_test()
    else: parser.print_help()

if __name__ == "__main__":
    main()
