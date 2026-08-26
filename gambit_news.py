# gambit_news.py - Gambits schacknyhetssystem
# Det här är den enda skriptfilen; det är den GitHub-flödet kör.

import os
import json
import time
import re
import logging
import random
import smtplib
import webbrowser
import threading
import requests
import glob
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, redirect, url_for
from requests.auth import HTTPBasicAuth
import base64

# === TVINGA IPv4 ===
# gambit.se svarar numera även på IPv6, men GitHubs servrar saknar IPv6-koppling.
# Utan det här försöker Python nå gambit.se via IPv6 först och får "Network is
# unreachable" — vilket den 11 augusti 2026 stoppade hela publiceringen.
# Vi ber därför nätverkslagret att bara slå upp IPv4-adresser.
try:
    import socket
    import urllib3.util.connection as _urllib3_conn
    _urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
except Exception as _e:  # pragma: no cover
    pass

# === KONFIGURATION ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('multi_news.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === MILJÖVARIABLER ===
load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Rate limiting inställningar
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "6"))
BASE_DELAY = int(os.getenv("BASE_DELAY", "5"))

# E-post inställningar
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO") 
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# WordPress inställningar
WP_USER = os.getenv("WP_USER")
WP_PASS = os.getenv("WP_PASS")
WP_URL = os.getenv("WP_URL")

# Adressen till redaktionen, dit godkännandemejlet länkar. Tidigare pekade
# mejlet på http://127.0.0.1:5000 — den lokala testservern, som bara fungerar
# på den dator där skriptet körs och alltså aldrig från en telefon eller när
# skriptet körs på GitHub.
REDAKTION_URL = os.getenv("REDAKTION_URL", "https://gambit.se/redaktionen/")

# WordPress kategorimappning
CATEGORY_MAPPING = {
    'Chess.com': 'chess-com',
    'ChessBase': 'chessbase', 
    'ChessBase India': 'chessbase-india',
    'FIDE': 'fide',
    'Schack.se': 'svenska-schackforbundet',
    'Chessdom': 'chessdom',
    'Europe Echecs': 'europe-echecs',
    'TWIC': 'internationella-turneringar'
}

# Skrivregler som delas av båda prompterna nedan – den för en ensam artikel och
# den för flera källor som slås ihop. Ändras de här slår ändringen igenom på båda.
SCHACKTERMER = """SVENSKA SCHACKTERMER – använd dessa, inte ordagranna översättningar:
draw = remi · offer a draw = bjuda remi · resign = ge upp · round = rond
tiebreak = särspel · standings = ställningen · rating = rating (aldrig "betyg")
time trouble = tidsnöd · blunder = grov miss · move = drag · check = schack
checkmate = matt · stalemate = patt · piece = pjäs · pawn = bonde
knight = springare · bishop = löpare · rook = torn · queen = dam · king = kung
file = linje · rank = rad · square = ruta · fork = gaffel · pin = bindning
skewer = spett · discovered attack = avdragare · sacrifice = offer
passed pawn = fribonde · doubled pawns = dubbelbönder · castling = rockad
kingside = kungsflygeln · queenside = damflygeln · opening = öppning
middlegame = mittspel · endgame = slutspel · classical = klassiskt schack
rapid = snabbschack · blitz = blixt · Swiss system = schweizersystem
round robin = rundturnering · board (i lagmatch) = bord
the Candidates = Kandidatturneringen · world number one = världsetta
Chinese = kinesisk/kinesiska (aldrig "chinesiska") · Dutch = nederländsk

MÄSTERSKAP HETER SÅ HÄR PÅ SVENSKA — översätt aldrig ordagrant:
Women's World Rapid Championship = dam-VM i snabbschack
Women's World Blitz Championship = dam-VM i blixtschack
Women's World Championship = dam-VM · Women's European Championship = dam-EM
European Women's Team Championship = lag-EM för damer
World Junior Championship = junior-VM · European Youth = ungdoms-EM
World Senior = senior-VM · Swedish Championship = SM
Samma mönster för alla: dam-VM, dam-EM, junior-VM, lag-SM. Skriv aldrig
"Europamästerskapet för kvinnor" eller "Världsmästerskapet i snabbschack för damer".

PARTI OCH MATCH ÄR INTE SAMMA SAK:
- Ett PARTI är en enskild uppgörelse mellan två spelare. Engelskans "game".
- En MATCH består av flera partier. Antingen mellan två spelare — som VM-matchen
  över fjorton partier — eller mellan två lag med flera spelare i varje.
Skriv alltså "vann partiet mot Carlsen", inte "vann matchen", när det gäller en
enskild uppgörelse. Och "matchen slutade 2,5-1,5" när flera partier spelats."""

# Ladda Anthropic om API-nyckel finns
anthropic_client = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("✅ Claude API konfigurerad")
    except ImportError:
        logger.warning("⚠️ Anthropic-biblioteket inte installerat")
else:
    logger.warning("⚠️ ANTHROPIC_API_KEY inte satt")

# === MODELLVAL ===
# Historik: systemet stod stilla i flera månader eftersom modellnamnet var
# hårdkodat till en modell som pensionerades. Varje översättning gav 404 och
# alla artiklar kastades — utan att något larmade.
#
# Lösningen: en lista med reservmodeller. Om den första inte finns provas nästa,
# och den som fungerar används resten av körningen. Sätt CLAUDE_MODEL i .env
# för att styra vilken som provas först.
CLAUDE_MODELS = [
    m.strip() for m in (
        os.getenv("CLAUDE_MODEL", "") + ",claude-sonnet-5,claude-haiku-4-5,claude-opus-5"
    ).split(",") if m.strip()
]
_active_model = None
# Inställningar som den valda modellen inte längre accepterar. Fylls på automatiskt
# när API:et säger ifrån, så att t.ex. en pensionerad temperature-inställning inte
# stoppar hela körningen.
_slopade_parametrar = set()


def korta_vid_meningsslut(text, maxlangd):
    """Korta en text utan att hugga av den mitt i en mening.

    Klipper vid sista punkt, utropstecken eller frågetecken som ryms. Finns
    inget meningsslut alls klipps det vid sista hela ordet, med tre punkter.
    """
    if not text or len(text) <= maxlangd:
        return text

    kandidat = text[:maxlangd]

    slut = max(kandidat.rfind('. '), kandidat.rfind('! '), kandidat.rfind('? '))
    if kandidat.rstrip() and kandidat.rstrip()[-1] in '.!?':
        slut = max(slut, len(kandidat.rstrip()) - 1)

    # Godta bara ett meningsslut som inte kapar bort merparten av texten
    if slut > maxlangd * 0.4:
        return kandidat[:slut + 1].rstrip()

    sista_mellanslag = kandidat.rfind(' ')
    if sista_mellanslag > 0:
        return kandidat[:sista_mellanslag].rstrip() + '…'

    return kandidat.rstrip() + '…'


_OGILTIGA_XML_TECKEN = re.compile(
    '[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff﷐-﷯￾￿]'
)


def rensa_ogiltiga_xml_tecken(text):
    """Ta bort styrtecken som XML-parsern inte accepterar.

    RSS-flöden innehåller ibland enstaka ogiltiga tecken i en enda artikel
    (t.ex. inklistrat från Word). ET.fromstring kraschar då på HELA flödet
    med "not well-formed (invalid token)" - inte bara den artikeln. Det gav
    Schack.se noll artiklar 2026-08-22 trots att flödet annars var normalt.
    """
    return _OGILTIGA_XML_TECKEN.sub('', text)


def dela_upp_svar(claude_text):
    """Plocka isär RUBRIK, TYP och TEXT ur Claudes svar.

    TYP-raden är valfri i praktiken — saknas den blir typen "Övrigt" i stället
    för att artikeln faller bort. Rubrik och text är det som måste finnas.
    """
    GILTIGA = ['Resultat', 'Porträtt', 'Pedagogik', 'Schackpolitik',
               'Teknik och AI', 'Historia och kultur', 'Övrigt']

    rubrik, typ, text = '', 'Övrigt', claude_text

    if 'TEXT:' in claude_text:
        huvud, text = claude_text.split('TEXT:', 1)
        text = text.strip()
        for rad in huvud.splitlines():
            rad = rad.strip()
            if rad.upper().startswith('RUBRIK:'):
                rubrik = rad.split(':', 1)[1].strip()
            elif rad.upper().startswith('TYP:'):
                kandidat = rad.split(':', 1)[1].strip()
                for g in GILTIGA:
                    if g.lower() in kandidat.lower():
                        typ = g
                        break
    else:
        rader = claude_text.split('\n', 1)
        rubrik = rader[0].strip()
        text = rader[1].strip() if len(rader) > 1 else claude_text

    return rubrik, typ, text


def hamta_text(response):
    """Plocka ut själva svarstexten ur Claudes svar.

    Nyare modeller tänker innan de svarar, och lägger då ett tankeblock först i
    svaret. Den gamla koden tog blindt första blocket och kraschade med
    "'ThinkingBlock' object has no attribute 'text'". Här letas i stället upp
    det första blocket som faktiskt innehåller text.
    """
    delar = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            if getattr(block, "type", "text") == "thinking":
                continue
            text = getattr(block, "text", None)
            if text:
                delar.append(text)

    if not delar:
        raise ValueError("Claudes svar innehöll ingen text")

    return "\n".join(delar).strip()


def claude_message(**kwargs):
    """Anropa Claude och anpassa sig automatiskt om modellen bytt förutsättningar.

    Hanterar två saker som annars stoppar hela nyhetsflödet:
      * modellen finns inte längre (404) → provar nästa modell i listan
      * en inställning stöds inte längre (400) → släpper den och försöker igen
    """
    global _active_model

    candidates = [_active_model] if _active_model else list(CLAUDE_MODELS)
    last_error = None

    for model in candidates:
        params = {k: v for k, v in kwargs.items() if k not in _slopade_parametrar}
        try:
            response = anthropic_client.messages.create(model=model, **params)
            if _active_model != model:
                logger.info(f"🤖 Använder modell: {model}")
                _active_model = model
            return response
        except Exception as e:
            text = str(e)

            if "not_found" in text or "404" in text:
                logger.warning(f"⚠️ Modellen {model} finns inte längre – provar nästa")
                last_error = e
                continue

            # T.ex: "`temperature` is deprecated for this model."
            traff = re.search(r"[`'\"](\w+)[`'\"] is (?:deprecated|not supported|unsupported)", text)
            if not traff:
                # T.ex: "Messages.create() got an unexpected keyword argument
                # 'temperature'." - det SDK:t kastar lokalt (innan anropet ens
                # skickas) när paketet uppgraderats och inte längre känner igen
                # parametern, i stället för ett vanligt API-felsvar. Samma
                # åtgärd gäller: släpp parametern och kör vidare utan den.
                traff = re.search(r"unexpected keyword argument [`'\"](\w+)[`'\"]", text)
            if traff and traff.group(1) not in _slopade_parametrar:
                parameter = traff.group(1)
                _slopade_parametrar.add(parameter)
                logger.warning(
                    f"⚠️ Inställningen '{parameter}' stöds inte av {model} – "
                    f"kör vidare utan den"
                )
                return claude_message(**kwargs)

            logger.error(f"❌ Oväntat fel från modellen {model}: {text[:300]}")
            raise

    # Om den tidigare fungerande modellen slutat fungera: gå igenom hela listan igen
    if _active_model:
        _active_model = None
        return claude_message(**kwargs)

    raise RuntimeError(
        f"Ingen av modellerna {CLAUDE_MODELS} kunde användas. Senaste fel: {last_error}"
    )

# USER AGENTS
USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

# === BASKLASSE ===
class NewsSource(ABC):
    def __init__(self, name, base_url, tag_name, enabled=True):
        self.name = name
        self.base_url = base_url
        self.tag_name = tag_name
        self.enabled = enabled
        self.request_delay = BASE_DELAY
        self.last_request_time = 0
        self.requests_this_minute = []
        self.total_requests = 0
        self.successful_requests = 0
        self.blocked_requests = 0
        self.response_times = []
        
    def get_random_headers(self):
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5,sv;q=0.3',
            'Connection': 'keep-alive',
        }
    
    def wait_for_rate_limit(self):
        now = time.time()
        self.requests_this_minute = [
            req_time for req_time in self.requests_this_minute 
            if now - req_time < 60
        ]
        
        if len(self.requests_this_minute) >= MAX_REQUESTS_PER_MINUTE:
            wait_time = 60 - (now - self.requests_this_minute[0])
            if wait_time > 0:
                time.sleep(wait_time)
        
        elapsed = now - self.last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        
        time.sleep(random.uniform(0.5, 2.0))
    
    def safe_request_with_backoff(self, url, max_retries=3, timeout=15):
        for attempt in range(max_retries):
            try:
                self.wait_for_rate_limit()
                
                start_time = time.time()
                headers = self.get_random_headers()
                response = requests.get(url, headers=headers, timeout=timeout)
                
                response_time = time.time() - start_time
                self.response_times.append(response_time)
                self.total_requests += 1
                self.last_request_time = time.time()
                self.requests_this_minute.append(self.last_request_time)
                
                if response.status_code == 200:
                    self.successful_requests += 1
                    return response
                elif response.status_code == 429:
                    self.blocked_requests += 1
                    wait_time = (2 ** attempt) + random.uniform(1, 3)
                    time.sleep(wait_time)
                    continue
                else:
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    logger.warning(f"⚠️ {self.name}: {url} svarade {response.status_code} – ger upp")
                    return None
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                logger.warning(f"⚠️ {self.name}: kunde inte hämta {url} – {type(e).__name__}: {e}")
        
        self.blocked_requests += 1
        return None
    
    def log_statistics(self):
        if self.total_requests > 0:
            success_rate = (self.successful_requests / self.total_requests) * 100
            avg_response_time = sum(self.response_times) / len(self.response_times) if self.response_times else 0
            logger.info(f"📊 {self.name}: {self.successful_requests}/{self.total_requests} OK ({success_rate:.1f}%), avg {avg_response_time:.2f}s")
    
    @abstractmethod
    def fetch_articles(self):
        pass
    
    @abstractmethod
    def parse_article_content(self, article_url):
        pass

    def text_ur_stycken(self, soup, minsta=250):
        """Sista utvägen när sidans egna klassnamn inte känns igen.

        Sajter byter layout och klassnamn med jämna mellanrum, och då slutar
        listor med selektorer att fungera. Brödtext ligger däremot nästan alltid
        i <p>-taggar. Här plockas alla stycken av rimlig längd, vilket sållar
        bort menyer, bildtexter och knappar utan att veta något om sidan.
        """
        stycken = []
        for p in soup.find_all('p'):
            t = p.get_text(" ", strip=True)
            if len(t) >= 40:
                stycken.append(t)
        text = "\n".join(stycken).strip()
        return text if len(text) >= minsta else None

    def las_artikeltext(self, article_url, selektorer):
        """Hämta sidan, prova selektorerna, fall annars tillbaka på styckena."""
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in selektorer:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True, separator="\n")
                if len(text) >= 250:
                    return text
        return self.text_ur_stycken(soup)

# === CHESS.COM KÄLLA ===
class ChesscomSource(NewsSource):
    def __init__(self):
        super().__init__("Chess.com", "https://www.chess.com/news", "Chess.com", True)
        self.request_delay = 4
    
    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles
                
            soup = BeautifulSoup(resp.text, "html.parser")
            all_links = soup.find_all('a', href=True)
            news_links = []
            
            for link in all_links:
                href = link.get('href', '')
                if '/news/view/' in href:
                    if not href.startswith('http'):
                        url = 'https://www.chess.com' + href
                    else:
                        url = href
                    news_links.append((url, link))
            
            seen_urls = set()
            unique_links = []
            for url, link in news_links:
                if url not in seen_urls:
                    seen_urls.add(url)
                    unique_links.append((url, link))
            
            logger.info(f"🔍 {self.name}: Hittade {len(unique_links)} unika artiklar")
            
            for url, link in unique_links:
                title = self._extract_title_from_link(link)
                
                if title and len(title) > 10 and len(title) < 200:
                    date = self._extract_date_from_url(url)
                    
                    articles.append({
                        "source": self.name,
                        "url": url,
                        "title": title,
                        "date": date,
                        "tag": self.tag_name
                    })
                    
                    if len(articles) >= 15:
                        break
                    
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1
        
        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles
    
    def _extract_title_from_link(self, link):
        title = link.get_text(strip=True)
        if title and len(title) > 10 and title != '...':
            return title
        
        parent = link.parent
        if parent:
            parent_text = parent.get_text(strip=True)
            if parent_text and len(parent_text) > 10 and len(parent_text) < 200:
                clean_text = ' '.join(parent_text.split())
                if clean_text != title:
                    return clean_text
        
        href = link.get('href', '')
        if href:
            url_parts = href.split('/')
            if url_parts:
                last_part = url_parts[-1]
                title_from_url = last_part.replace('-', ' ').replace('_', ' ')
                if len(title_from_url) > 10:
                    return title_from_url.title()
        
        return None
    
    def _extract_date_from_url(self, url):
        return (datetime.now() - timedelta(days=1)).isoformat()
    
    def parse_article_content(self, article_url):
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None
            
        soup = BeautifulSoup(resp.text, "html.parser")
        
        content_selectors = [
            '[class*="article-body"]',
            '[class*="news-content"]', 
            'article',
            '.content'
        ]
        
        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                content = content_element.get_text(strip=True, separator="\n")
                if len(content) > 100:
                    return content
        return None

# === CHESSBASE KÄLLA ===
class ChessBaseSource(NewsSource):
    def __init__(self):
        # RSS-flödet i stället för att skrapa länkarna på startsidan. Den gamla
        # metoden letade efter <a href> som innehöll "/post/" och satte alltid
        # datumet till "igår". Den gav nästan inga nya artiklar i praktiken -
        # bara 3 på en hel vecka, och en av dem hade inget riktigt
        # artikelinnehåll när Claude skulle skriva om den. RSS-flödet ger
        # riktiga publiceringsdatum och en sammanfattning som fallback om
        # sidan inte går att hämta (se _rss_content).
        super().__init__("ChessBase", "https://en.chessbase.com/feed", "ChessBase", True)
        self.request_delay = 5

    def fetch_articles(self):
        import xml.etree.ElementTree as ET
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []

        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles

            root = ET.fromstring(rensa_ogiltiga_xml_tecken(resp.text))
            poster = root.findall('.//item')
            logger.info(f"🔍 {self.name}: Hittade {len(poster)} artiklar i RSS")

            # ChessBases flöde är inte begränsat till de senaste - det sträcker
            # sig 18 månader bakåt (100+ poster). Upptäcktes 2026-08-22 när en
            # körning plötsligt drog in hela historiken på en gång. Övriga
            # RSS-källor (Schack.se, TWIC) har naturligt små flöden och behöver
            # inget tak, men ChessBase och Chessdom får ett explicit tak här.
            if len(poster) > 15:
                logger.info(f"✂️ {self.name}: Begränsar till de 15 senaste (av {len(poster)})")
            poster = poster[:15]

            for item in poster:
                titel = (item.findtext('title') or '').strip()
                url   = (item.findtext('link') or '').strip()
                datum = item.findtext('pubDate') or datetime.now().isoformat()
                beskr = item.findtext('description') or ''
                ren   = re.sub(r'<[^>]+>', ' ', beskr)
                ren   = re.sub(r'\s+', ' ', ren).strip()

                if titel and url:
                    articles.append({
                        "source": self.name,
                        "url": url,
                        "title": titel,
                        "date": datum,
                        "tag": self.tag_name,
                        "_rss_content": ren,
                    })

        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1

        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles

    def parse_article_content(self, article_url):
        return self.las_artikeltext(article_url, [
            '.cb-article-content', '.newsText', '.article-content', '.content',
        ])

# === FÖRBÄTTRAD FIDE KÄLLA ===
class FideSource(NewsSource):
    def __init__(self):
        super().__init__("FIDE", "https://www.fide.com/news", "FIDE", True)
        self.request_delay = 6
    
    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            # Testa flera FIDE URLs
            urls_to_try = [
                "https://www.fide.com/news",
                "https://www.fide.com/fide-news", 
                "https://www.fide.com/"
            ]
            
            for url in urls_to_try:
                resp = self.safe_request_with_backoff(url)
                if not resp:
                    continue
                    
                soup = BeautifulSoup(resp.text, "html.parser")
                logger.info(f"🔍 {self.name}: Söker artiklar på {url}")
                
                # Mer omfattande sökning efter FIDE-artiklar
                potential_links = soup.find_all('a', href=True)
                seen_urls = set()
                
                for link in potential_links:
                    href = link.get('href', '')
                    text = link.get_text(strip=True)
                    
                    # Sök efter FIDE-relaterade länkar
                    if any(pattern in href.lower() for pattern in [
                        'news', 'article', 'announcement', 'press', 'world-championship',
                        'grand-swiss', 'candidates', 'olympiad', 'circuit', 'fide'
                    ]):
                        if not href.startswith('http'):
                            full_url = 'https://www.fide.com' + href
                        else:
                            full_url = href
                        
                        if full_url in seen_urls or len(full_url) < 25:
                            continue
                        seen_urls.add(full_url)
                        
                        if text and len(text) > 15 and len(text) < 200:
                            # Kontrollera att det inte är navigation
                            if not any(nav_word in text.lower() for nav_word in [
                                'home', 'contact', 'about', 'login', 'register', 'menu',
                                'search', 'directory', 'officials', 'handbook'
                            ]):
                                articles.append({
                                    "source": self.name,
                                    "url": full_url,
                                    "title": text,
                                    "date": (datetime.now() - timedelta(days=1)).isoformat(),
                                    "tag": self.tag_name
                                })
                                
                                if len(articles) >= 10:
                                    break
                
                if len(articles) > 0:
                    break  # Om vi hittade artiklar, sluta söka
                                    
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1
            
        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles
    
    def parse_article_content(self, article_url):
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None
            
        soup = BeautifulSoup(resp.text, "html.parser")
        
        content_selectors = [
            '.news-content',
            '.article-content', 
            '.content-main',
            'article',
            '.content',
            'main',
            '.post-content',
            '.entry-content'
        ]
        
        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                content = content_element.get_text(strip=True, separator="\n")
                if len(content) > 100:
                    return content
        
        # Fallback - ta bara all text från body
        body = soup.find('body')
        if body:
            content = body.get_text(strip=True, separator="\n")
            if len(content) > 200:
                return content[:1500]  # Begränsa till rimlig längd
                
        return None

# === FÖRBÄTTRAD SCHACK.SE KÄLLA ===  
class SchackSeSource(NewsSource):
    def __init__(self):
        # RSS-flödet i stället för att leta i sidans HTML. Den gamla metoden
        # skannade div-taggar efter ord som "schack" och "turnering" och
        # plockade de första 80 tecknen som rubrik. Den gav antingen skräp
        # eller ingenting, och satte alltid datumet till "igår".
        super().__init__("Schack.se", "https://schack.se/feed/", "Svenska Schackförbundet", True)
        self.request_delay = 3

    def fetch_articles(self):
        import xml.etree.ElementTree as ET
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles

            root = ET.fromstring(rensa_ogiltiga_xml_tecken(resp.text))
            poster = root.findall('.//item')
            logger.info(f"🔍 {self.name}: Hittade {len(poster)} artiklar i RSS")

            for item in poster:
                titel = (item.findtext('title') or '').strip()
                url   = (item.findtext('link') or '').strip()
                datum = item.findtext('pubDate') or datetime.now().isoformat()
                beskr = item.findtext('description') or ''
                ren   = re.sub(r'<[^>]+>', ' ', beskr)
                ren   = re.sub(r'\s+', ' ', ren).strip()

                if titel and url:
                    articles.append({
                        "source": self.name,
                        "url": url,
                        "title": titel,
                        "date": datum,
                        "tag": self.tag_name,
                        "_rss_content": ren,
                    })
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1

        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles

    def parse_article_content(self, article_url):
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        content_selectors = [
            '.article-content',
            '.news-content',
            '.post-content',
            '.entry-content',
            'article',
            '.content',
            'main'
        ]

        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                content = content_element.get_text(strip=True, separator="\n")
                if len(content) > 100:
                    return content
        return None

# === CHESSBASE INDIA KÄLLA ===
class ChessBaseIndiaSource(NewsSource):
    def __init__(self):
        # /news gav "page not found" från och med augusti 2026. Artiklarna listas på förstasidan.
        super().__init__("ChessBase India", "https://www.chessbase.in/", "ChessBase India", True)
        self.request_delay = 5
    
    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles
                
            soup = BeautifulSoup(resp.text, "html.parser")
            all_links = soup.find_all('a', href=True)
            
            seen_urls = set()
            
            for link in all_links:
                href = link.get('href')
                if href and '/news/' in href:
                    url = urljoin('https://www.chessbase.in/', href)
                    if 'chessbase.in' not in url:
                        continue
                    
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    
                    title = link.get_text(strip=True)
                    if not title or len(title) < 10:
                        if link.parent:
                            title = link.parent.get_text(strip=True)
                    
                    if title and len(title) > 15 and len(title) < 200:
                        date = (datetime.now() - timedelta(days=1)).isoformat()
                        
                        articles.append({
                            "source": self.name,
                            "url": url,
                            "title": title,
                            "date": date,
                            "tag": self.tag_name
                        })
                        
                        if len(articles) >= 12:
                            break
                    
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1
        
        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles
    
    def parse_article_content(self, article_url):
        # Sidans klassnamn stämde inte längre, vilket gav "För kort innehåll"
        # på varenda artikel. Nu provas selektorerna först och styckena sedan.
        return self.las_artikeltext(article_url, [
            'article', '.article-content', '.news-content',
            '.post-content', '.entry-content', 'main',
        ])

# === CHESSDOM KÄLLA ===
class ChessdomSource(NewsSource):
    def __init__(self):
        # RSS-flödet i stället för att skrapa startsidans länkar. Den gamla
        # metoden matchade i praktiken vilken länk som helst med "chessdom.com"
        # i href eller som började med "/" - lika gärna navigering som artikel
        # - och fastnade dessutom på samma fästa turneringssidor dag efter dag,
        # så inga nya artiklar kom igenom (0 nya på en hel vecka). RSS-flödet
        # innehåller hela artikeltexten i <content:encoded>, så vi slipper
        # hämta artikelsidan en gång till för att få innehållet.
        super().__init__("Chessdom", "https://www.chessdom.com/feed/", "Chessdom", True)
        self.request_delay = 4

    def fetch_articles(self):
        import xml.etree.ElementTree as ET
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        NS = {'content': 'http://purl.org/rss/1.0/modules/content/'}

        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles

            root = ET.fromstring(rensa_ogiltiga_xml_tecken(resp.text))
            poster = root.findall('.//item')
            logger.info(f"🔍 {self.name}: Hittade {len(poster)} artiklar i RSS")

            # Säkerhetstak, samma som ChessBase - ifall Chessdom någon gång
            # skulle byta till ett flöde med mer historik än en vanlig
            # WordPress-standard (för närvarande ~10 poster).
            if len(poster) > 15:
                logger.info(f"✂️ {self.name}: Begränsar till de 15 senaste (av {len(poster)})")
            poster = poster[:15]

            for item in poster:
                titel = (item.findtext('title') or '').strip()
                url   = (item.findtext('link') or '').strip()
                datum = item.findtext('pubDate') or datetime.now().isoformat()

                fulltext = item.findtext('content:encoded', namespaces=NS) or ''
                beskr = item.findtext('description') or ''
                ren = re.sub(r'<[^>]+>', ' ', fulltext or beskr)
                ren = re.sub(r'\s+', ' ', ren).strip()

                if titel and url:
                    articles.append({
                        "source": self.name,
                        "url": url,
                        "title": titel,
                        "date": datum,
                        "tag": self.tag_name,
                        "_rss_content": ren,
                    })

        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1

        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles

    def parse_article_content(self, article_url):
        # RSS-flödet ger oss redan hela artikeln (_rss_content ovan), men vi
        # försöker ändå läsa artikelsidan direkt först - lyckas det inte
        # faller översättningssteget automatiskt tillbaka på flödestexten.
        # Chessdom använder inte de vanliga WordPress-klasserna: deras egen
        # behållare heter post-wrap-out1 (kontrollerat 2026-08-09).
        return self.las_artikeltext(article_url, [
            '.post-wrap-out1', '.entry-content', '.article-content',
            '.post-content', '.content',
        ])

# === EUROPE ECHECS KÄLLA ===
class EuropeEchecsSource(NewsSource):
    def __init__(self):
        super().__init__("Europe Echecs", "https://www.europe-echecs.com/", "Europe Echecs", True)
        self.request_delay = 5
    
    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles
                
            soup = BeautifulSoup(resp.text, "html.parser")
            all_links = soup.find_all('a', href=True)
            
            seen_urls = set()
            
            for link in all_links:
                href = link.get('href')
                # Länkarna på förstasidan är relativa (/art/...), så kravet på att
                # domänen skulle stå i adressen gjorde att ingenting matchade.
                if href and '/art/' in href:
                    url = urljoin('https://www.europe-echecs.com/', href)
                    if 'europe-echecs.com' not in url:
                        continue
                    
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    
                    title = link.get_text(strip=True)
                    if title and len(title) > 15 and len(title) < 200:
                        articles.append({
                            "source": self.name,
                            "url": url,
                            "title": title,
                            "date": (datetime.now() - timedelta(days=1)).isoformat(),
                            "tag": self.tag_name
                        })
                        
                        if len(articles) >= 8:
                            break
                    
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1
        
        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles
    
    def parse_article_content(self, article_url):
        return self.las_artikeltext(article_url, [
            'article', '.article-content', '.node-content',
            '.field-item', '.content', 'main',
        ])

# === THE WEEK IN CHESS ===
# Flyttad hit från gambit_news_complete.py när den filen togs bort 11 aug 2026.
# Hämtar via RSS i stället för att läsa förstasidan, vilket är stabilare —
# ett flöde ändrar sig sällan, till skillnad från en sidlayout.
class TWICSource(NewsSource):
    def __init__(self):
        super().__init__("TWIC", "https://theweekinchess.com/twic-rss-feed", "TWIC", True)
        self.request_delay = 2

    def fetch_articles(self):
        import xml.etree.ElementTree as ET
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []

        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles

            root = ET.fromstring(rensa_ogiltiga_xml_tecken(resp.text))
            items = root.findall('.//item')
            logger.info(f"🔍 {self.name}: Hittade {len(items)} artiklar i RSS")

            for item in items:
                title = item.findtext('title') or ''
                url = item.findtext('link') or item.findtext('guid') or ''
                date = item.findtext('pubDate') or datetime.now().isoformat()
                desc = item.findtext('description') or ''
                clean_desc = re.sub(r'<[^>]+>', ' ', desc).strip()
                clean_desc = re.sub(r'\s+', ' ', clean_desc)

                if title and url and len(title) > 5:
                    articles.append({
                        "source": self.name,
                        "url": url,
                        "title": title,
                        "date": date,
                        "tag": self.tag_name,
                        "_rss_content": clean_desc
                    })

        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")

        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles

    def parse_article_content(self, article_url):
        return self.las_artikeltext(article_url, [
            'article', '.entry-content', '.post-content', '#content', 'main',
        ])

# === WORDPRESS PUBLISHER MED KATEGORIER ===
class WordPressPublisher:
   def __init__(self):
       self.wp_url = WP_URL
       self.wp_user = WP_USER  
       self.wp_pass = WP_PASS
       
   def get_category_id(self, source_name):
       """Skapa eller hämta kategori-ID baserat på källa"""
       try:
           category_slug = CATEGORY_MAPPING.get(source_name, 'allmant')
           
           # Hämta befintliga kategorier
           categories_url = f"{self.wp_url}/wp-json/wp/v2/categories"
           response = requests.get(categories_url, auth=HTTPBasicAuth(self.wp_user, self.wp_pass))
           
           if response.status_code == 200:
               categories = response.json()
               
               # Leta efter befintlig kategori
               for cat in categories:
                   if cat['slug'] == category_slug:
                       logger.info(f"✅ Hittade befintlig kategori: {source_name} (ID: {cat['id']})")
                       return cat['id']
               
               # Skapa ny kategori om den inte finns
               new_category = {
                   'name': source_name,
                   'slug': category_slug,
                   'description': f'Artiklar från {source_name}'
               }
               
               create_response = requests.post(
                   categories_url,
                   json=new_category,
                   auth=HTTPBasicAuth(self.wp_user, self.wp_pass)
               )
               
               if create_response.status_code == 201:
                   new_cat = create_response.json()
                   logger.info(f"✅ Skapade ny kategori: {source_name} (ID: {new_cat['id']})")
                   return new_cat['id']
           
           logger.warning(f"⚠️ Kunde inte hantera kategorier, använder standard (ID: 1)")
           return 1  # Fallback till standard kategori
           
       except Exception as e:
           logger.warning(f"⚠️ Kunde inte hantera kategori för {source_name}: {e}")
           return 1
   
   def publish_article(self, selected_article, original_article):
       """Publicera artikel på WordPress med rätt kategori och AI-disclaimer"""
       if not all([self.wp_url, self.wp_user, self.wp_pass]):
           logger.warning("⚠️ WordPress-inställningar saknas")
           return False
           
       try:
           # Hämta kategori-ID för källan
           category_id = self.get_category_id(original_article['source'])
           
           api_url = f"{self.wp_url}/wp-json/wp/v2/posts"
           
           # Formatera innehåll med AI-disclaimer
           formatted_content = f"""
{selected_article['content']}

<hr style="margin: 20px 0; border: none; height: 1px; background: #ddd;">

<div style="background: #f9f9f9; padding: 15px; border-left: 4px solid #0073aa; margin: 15px 0;">
<p style="margin: 0; font-style: italic; color: #666;">
<strong>ℹ️ Om denna artikel:</strong> Denna artikel är översatt och bearbetad från originalkällan med hjälp av AI (Claude). 
<br>📎 <strong>Källa:</strong> <a href="{original_article['original_url']}" target="_blank" rel="noopener">{original_article['source']}</a>
</p>
</div>
"""
           
           post_data = {
               'title': selected_article['title'],
               'content': formatted_content,
               'status': 'publish',
               'categories': [category_id],
               'excerpt': selected_article['content'][:150] + '...',
               'tags': [original_article['source'].lower().replace(' ', '-'), 'ai-översatt'],
               'meta': {
                   'source_url': original_article['original_url'],
                   'source_name': original_article['source'],
                   'ai_translated': True
               }
           }
           
           response = requests.post(
               api_url,
               json=post_data,
               auth=HTTPBasicAuth(self.wp_user, self.wp_pass),
               headers={'Content-Type': 'application/json'},
               timeout=30
           )
           
           if response.status_code == 201:
               post_data = response.json()
               post_id = post_data.get('id')
               post_url = post_data.get('link', '')
               logger.info(f"✅ Artikel publicerad: {selected_article['title']}")
               logger.info(f"   📂 Kategori: {original_article['source']} (ID: {category_id})")
               logger.info(f"   🔗 URL: {post_url}")
               return True
           else:
               logger.error(f"❌ WordPress-fel: {response.status_code} - {response.text}")
               logger.error(f"❌ Misslyckades att publicera till WordPress. Data: {json.dumps(post_data, ensure_ascii=False)[:500]}")
               logger.error(f"❌ WordPress-url: {api_url}")
               logger.error(f"❌ WordPress-user: {self.wp_user}")
               logger.error(f"❌ WordPress-pass: {self.wp_pass[:2]}***")
               logger.error(f"❌ WordPress-kategori: {category_id}")
               logger.error(f"❌ WordPress-headers: {response.headers}")
               logger.error(f"❌ WordPress-request: {response.request.body}")
               return False
               
       except Exception as e:
           logger.error(f"❌ Kunde inte publicera artikel: {e}")
           return False

# === FÖRBÄTTRAT E-POST OCH WEBBGRÄNSSNITT MED "HOPPA ÖVER" ===
class EmailApprovalSystem:
   def __init__(self):
       self.app = Flask(__name__)
       self.setup_routes()
       
   def setup_routes(self):
       @self.app.route('/')
       def index():
           return self.show_articles_for_approval()
           
       @self.app.route('/process', methods=['POST'])
       def process_articles():
           return self.handle_article_processing()
   
   def show_articles_for_approval(self):
       """Visa artiklar för godkännande med förbättrat gränssnitt"""
       approval_files = glob.glob("pending_approval_*.json")
       if not approval_files:
           return "<h1>Inga artiklar att granska</h1>"
       
       latest_file = max(approval_files)
       
       with open(latest_file, 'r', encoding='utf-8') as f:
           articles = json.load(f)
       
       html = f"""
<!DOCTYPE html>
<html>
<head>
   <title>Schackartiklar - Godkännande</title>
   <meta charset="utf-8">
   <style>
       body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
       .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
       .article {{ border: 1px solid #ddd; margin: 15px 0; padding: 15px; border-radius: 8px; background: #fafafa; transition: all 0.3s ease; }}
       .article.publish {{ background: #e8f5e8; border-color: #4CAF50; }}
       .article.skip {{ background: #fff3e0; border-color: #FF9800; }}
       .article-header {{ display: flex; align-items: center; margin-bottom: 10px; }}
       .article-radio {{ margin-right: 10px; transform: scale(1.3); }}
       .article-source {{ background: #2196F3; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; margin-right: 10px; }}
       .article-title {{ font-weight: bold; font-size: 18px; color: #333; width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
       .article-content {{ margin: 10px 0; width: 100%; min-height: 150px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-family: Arial, sans-serif; }}
       .article-url {{ font-size: 12px; color: #666; margin-top: 10px; }}
       .controls {{ position: fixed; bottom: 20px; right: 20px; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
       .btn {{ padding: 12px 24px; margin: 0 5px; border: none; border-radius: 6px; cursor: pointer; font-size: 16px; transition: background-color 0.3s; }}
       .btn-primary {{ background: #4CAF50; color: white; }}
       .btn-primary:hover {{ background: #45a049; }}
       .btn-secondary {{ background: #2196F3; color: white; }}
       .btn-secondary:hover {{ background: #1976D2; }}
       .btn-warning {{ background: #FF9800; color: white; }}
       .btn-warning:hover {{ background: #F57C00; }}
       .stats {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
       .expand-btn {{ background: #FF9800; color: white; padding: 5px 10px; border: none; border-radius: 4px; cursor: pointer; margin-top: 5px; }}
       .ai-notice {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px; color: #856404; }}
       .action-buttons {{ margin: 15px 0; padding: 10px; background: #f0f0f0; border-radius: 5px; }}
       .action-label {{ font-weight: bold; margin-right: 15px; color: #333; }}
       .radio-group {{ display: inline-flex; gap: 20px; }}
       .radio-item {{ display: flex; align-items: center; gap: 5px; }}
       .header {{ text-align: center; margin-bottom: 30px; }}
       .header h1 {{ color: #333; margin: 0; }}
       .quick-actions {{ margin: 20px 0; text-align: center; }}
       .quick-btn {{ margin: 0 10px; }}
   </style>
   <script>
       function setAction(articleId, action) {{
           const article = document.getElementById('article-' + articleId);
           const radio = document.querySelector(`input[name="action-${{articleId}}"][value="${{action}}"]`);
           if (radio) radio.checked = true;
           
           article.className = 'article ' + action;
           document.getElementById('action-' + articleId).value = action;
           updateStats();
       }}
       
       function expandContent(articleId) {{
           const content = document.getElementById('content-' + articleId);
           content.style.minHeight = '300px';
           document.getElementById('expand-btn-' + articleId).style.display = 'none';
       }}
       
       function updateStats() {{
           const publish = document.querySelectorAll('input[value="publish"]:checked').length;
           const skip = document.querySelectorAll('input[value="skip"]:checked').length;
           const total = document.querySelectorAll('.article').length;
           
           document.getElementById('publish-count').textContent = publish;
           document.getElementById('skip-count').textContent = skip;
           document.getElementById('total-count').textContent = total;
           document.getElementById('remaining-count').textContent = total - publish - skip;
       }}
       
       function selectAllForPublish() {{
           const articles = document.querySelectorAll('.article');
           articles.forEach((article, index) => {{
               setAction(index, 'publish');
           }});
       }}
       
       function selectAllForSkip() {{
           const articles = document.querySelectorAll('.article');
           articles.forEach((article, index) => {{
               setAction(index, 'skip');
           }});
       }}
       
       function clearAllSelections() {{
           const articles = document.querySelectorAll('.article');
           articles.forEach((article, index) => {{
               article.className = 'article';
               const radios = document.querySelectorAll(`input[name="action-${{index}}"]`);
               radios.forEach(radio => radio.checked = false);
               document.getElementById('action-' + index).value = '';
           }});
           updateStats();
       }}
       
       function processArticles() {{
           const toPublish = [];
           const toSkip = [];
           
           document.querySelectorAll('input[name^="action-"]:checked').forEach(radio => {{
               const articleId = parseInt(radio.name.split('-')[1]);
               const action = radio.value;
               
               if (action === 'publish') {{
                   const title = document.getElementById('title-' + articleId).value;
                   const content = document.getElementById('content-' + articleId).value;
                   toPublish.push({{ id: articleId, title: title, content: content }});
               }} else if (action === 'skip') {{
                   toSkip.push({{ id: articleId }});
               }}
           }});
           
           if (toPublish.length === 0 && toSkip.length === 0) {{
               alert('⚠️ Välj åtgärd för minst en artikel!');
               return;
           }}
           
           const message = `🚀 Publicera ${{toPublish.length}} artiklar och hoppa över ${{toSkip.length}} artiklar?`;
           
           if (confirm(message)) {{
               const processBtn = document.getElementById('process-btn');
               processBtn.innerHTML = '⏳ Bearbetar...';
               processBtn.disabled = true;
               
               fetch('/process', {{
                   method: 'POST',
                   headers: {{'Content-Type': 'application/json'}},
                   body: JSON.stringify({{ publish: toPublish, skip: toSkip }})
               }})
               .then(response => response.json())
               .then(data => {{
                   if (data.success) {{
                       alert(`🎉 ${{data.published}} artiklar publicerade! ${{data.skipped}} artiklar borttagna.`);
                       location.reload();
                   }} else {{
                       alert('❌ Fel: ' + data.error);
                       processBtn.innerHTML = '🚀 Bearbeta artiklar';
                       processBtn.disabled = false;
                   }}
               }})
               .catch(error => {{
                   alert('❌ Nätverksfel: ' + error);
                   processBtn.innerHTML = '🚀 Bearbeta artiklar';
                   processBtn.disabled = false;
               }});
           }}
       }}
   </script>
</head>
<body>
   <div class="container">
       <div class="header">
           <h1>🔥 Schackartiklar för publicering på gambit.se</h1>
       </div>
       
       <div class="ai-notice">
           <strong>ℹ️ Observera:</strong> Dessa artiklar är översatta och bearbetade från originalkällor med hjälp av AI (Claude). 
           Kontrollera innehållet innan publicering. Artiklar publiceras automatiskt med kategorier baserat på källa.
       </div>
       
       <div class="stats">
           <strong>📊 Status:</strong> 
           <span style="background: rgba(255,255,255,0.2); padding: 5px 10px; border-radius: 15px; margin: 0 5px;">✅ Publicera: <span id="publish-count">0</span></span>
           <span style="background: rgba(255,255,255,0.2); padding: 5px 10px; border-radius: 15px; margin: 0 5px;">⏭️ Hoppa över: <span id="skip-count">0</span></span>
           <span style="background: rgba(255,255,255,0.2); padding: 5px 10px; border-radius: 15px; margin: 0 5px;">⏸️ Obeslutat: <span id="remaining-count">{len(articles)}</span></span>
           <span style="background: rgba(255,255,255,0.2); padding: 5px 10px; border-radius: 15px; margin: 0 5px;"><strong>Totalt: <span id="total-count">{len(articles)}</span></strong></span>
       </div>
       
       <div class="quick-actions">
           <button class="btn btn-primary quick-btn" onclick="selectAllForPublish()">✅ Välj alla för publicering</button>
           <button class="btn btn-warning quick-btn" onclick="selectAllForSkip()">⏭️ Hoppa över alla</button>
           <button class="btn btn-secondary quick-btn" onclick="clearAllSelections()">🔄 Rensa alla val</button>
       </div>
"""
       
       # Lägg till varje artikel med förbättrat gränssnitt
       for i, article in enumerate(articles):
           source_color = {
               'Chess.com': '#4CAF50',
               'ChessBase': '#FF9800', 
               'Schack.se': '#2196F3',
               'ChessBase India': '#9C27B0',
               'Chessdom': '#607D8B',
               'Europe Echecs': '#795548',
               'FIDE': '#FF5722'
           }.get(article['source'], '#666')
           
           title = article.get('swedish_title', article.get('original_title', 'Ingen titel'))
           content = article.get('swedish_content', article.get('content', 'Inget innehåll'))
           
           if len(content) > 800:
               content = content[:800] + "..."
           
           html += f"""
       <div class="article" id="article-{i}">
           <div class="article-header">
               <span class="article-source" style="background: {source_color}">{article['source']}</span>
           </div>
           
           <div class="action-buttons">
               <span class="action-label">Välj åtgärd:</span>
               <div class="radio-group">
                   <div class="radio-item">
                       <input type="radio" name="action-{i}" value="publish" class="article-radio" onchange="setAction({i}, 'publish')" id="publish-{i}">
                       <label for="publish-{i}">✅ Publicera</label>
                   </div>
                   <div class="radio-item">
                       <input type="radio" name="action-{i}" value="skip" class="article-radio" onchange="setAction({i}, 'skip')" id="skip-{i}">
                       <label for="skip-{i}">⏭️ Hoppa över</label>
                   </div>
               </div>
               <input type="hidden" id="action-{i}" value="">
           </div>
           
           <div style="margin-bottom: 10px;">
               <label><strong>Rubrik:</strong></label>
               <input type="text" id="title-{i}" class="article-title" value="{title.replace('"', '&quot;')}">
           </div>
           
           <div style="margin-bottom: 10px;">
               <label><strong>Innehåll:</strong></label>
               <button class="expand-btn" id="expand-btn-{i}" onclick="expandContent({i})">Expandera för längre text</button>
               <textarea id="content-{i}" class="article-content">{content.replace('<', '&lt;').replace('>', '&gt;')}</textarea>
           </div>
           
           <div class="article-url">
               📎 <a href="{article['original_url']}" target="_blank">Originalartikeln</a>
           </div>
       </div>
       """
       
       html += """
           <div class="controls">
               <button class="btn btn-primary" id="process-btn" onclick="processArticles()">🚀 Bearbeta artiklar</button>
           </div>
           
       </div>
       
       <script>updateStats();</script>
   </body>
   </html>
   """
       return html
   
   def handle_article_processing(self):
       """Hantera både publicering och borttagning av artiklar"""
       try:
           data = request.get_json()
           to_publish = data.get('publish', [])
           to_skip = data.get('skip', [])
           
           logger.info(f"📝 Bearbetar {len(to_publish)} artiklar för publicering, {len(to_skip)} för borttagning")
           
           # Ladda alla artiklar
           approval_files = glob.glob("pending_approval_*.json")
           if not approval_files:
               return jsonify({'success': False, 'error': 'Inga artiklar att bearbeta'})
           
           latest_file = max(approval_files)
           
           with open(latest_file, 'r', encoding='utf-8') as f:
               all_articles = json.load(f)
           
           published_count = 0
           
           # Publicera valda artiklar
           if to_publish and WP_URL and WP_USER and WP_PASS:
               wp_publisher = WordPressPublisher()
               for selected in to_publish:
                   if selected['id'] < len(all_articles):
                       original_article = all_articles[selected['id']]
                       if wp_publisher.publish_article(selected, original_article):
                           published_count += 1
                           logger.info(f"✅ Publicerade: {selected['title']}")
                       else:
                           logger.error(f"❌ Kunde inte publicera: {selected['title']}")
           elif to_publish:
               logger.warning("⚠️ WordPress inte konfigurerat - kan inte publicera artiklar")

           # Ta bort både publicerade och överhoppade artiklar från pending-filen
           processed_ids = [item['id'] for item in to_publish + to_skip]
           remaining_articles = [art for i, art in enumerate(all_articles) if i not in processed_ids]
           
           # Spara uppdaterad lista
           with open(latest_file, 'w', encoding='utf-8') as f:
               json.dump(remaining_articles, f, indent=2, ensure_ascii=False)
           
           # Logga också vilka artiklar som hoppades över
           if to_skip:
               logger.info(f"⏭️ Hoppade över {len(to_skip)} artiklar:")
               for skipped in to_skip:
                   if skipped['id'] < len(all_articles):
                       title = all_articles[skipped['id']].get('swedish_title', 'Okänd titel')
                       logger.info(f"   • {title}")
           
           return jsonify({
               'success': True,
               'published': published_count,
               'skipped': len(to_skip),
               'remaining': len(remaining_articles),
               'message': f'Publicerade {published_count} artiklar, hoppade över {len(to_skip)} artiklar'
           })
           
       except Exception as e:
           logger.error(f"❌ Fel vid bearbetning: {e}")
           return jsonify({'success': False, 'error': str(e)})
   
   def send_approval_email(self, articles_file):
       """Skicka e-post med länk för godkännande"""
       if not EMAIL_FROM or not EMAIL_TO or not EMAIL_PASSWORD:
           logger.warning("⚠️ E-postinställningar saknas i .env")
           return False
           
       try:
           with open(articles_file, 'r', encoding='utf-8') as f:
               articles = json.load(f)
           
           article_count = len(articles)
           
           msg = MIMEMultipart()
           msg['From'] = EMAIL_FROM
           msg['To'] = EMAIL_TO
           ord_artiklar = "artikel" if article_count == 1 else "artiklar"
           msg['Subject'] = f"{article_count} nya schack{ord_artiklar} väntar på godkännande"

           body = f"""Hej!

{article_count} nya schack{ord_artiklar} har samlats in och översatts och väntar på ditt godkännande.

Fördelning per källa:
"""

           by_source = {}
           for article in articles:
               source = article['source']
               by_source[source] = by_source.get(source, 0) + 1

           for source, count in sorted(by_source.items()):
               body += f"   {source}: {count}\n"

           body += f"""
Granska och välj artiklar här:
{REDAKTION_URL}
"""
           
           msg.attach(MIMEText(body, 'plain', 'utf-8'))
           
           server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
           server.starttls()
           server.login(EMAIL_FROM, EMAIL_PASSWORD)
           server.send_message(msg)
           server.quit()
           
           logger.info(f"📧 E-post skickat till {EMAIL_TO}")
           return True
           
       except Exception as e:
           logger.error(f"❌ Kunde inte skicka e-post: {e}")
           return False
   
def start_web_server(self):
    """Starta webbserver för godkännandegränssnitt"""
    import signal
    import sys
    
    def signal_handler(sig, frame):
        print("\n👋 Servern stängd")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    def run_server():
        self.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
    
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    threading.Timer(1.0, lambda: webbrowser.open('http://127.0.0.1:5000')).start()
    
    logger.info("🌐 Webbserver startad på http://127.0.0.1:5000")

# === HUVUDMOTOR MED ALLA FÖRBÄTTRINGAR ===
class MultiNewsEngine:
   def __init__(self):
       self.sources = [
           ChesscomSource(),
           ChessBaseSource(),          
           FideSource(),               # Förbättrad FIDE-källa
           SchackSeSource(),           # Förbättrad Schack.se-källa
           ChessBaseIndiaSource(),
           ChessdomSource(),
           EuropeEchecsSource(),
           TWICSource()               # The Week in Chess, via RSS
       ]
   
   def collect_from_all_sources(self):
       all_articles = []
       
       for source in self.sources:
           if source.enabled:
               logger.info(f"🔄 Bearbetar {source.name}...")
               articles = source.fetch_articles()
               all_articles.extend(articles)
               time.sleep(3)
       
       return all_articles
   
   def load_seen_urls(self):
       """Läs listan över artiklar som redan hanterats färdigt"""
       try:
           with open("seen_articles.json", "r", encoding='utf-8') as f:
               return set(json.load(f))
       except (FileNotFoundError, json.JSONDecodeError):
           return set()

   def mark_as_seen(self, articles):
       """Bocka av artiklar SOM FAKTISKT BEARBETATS FÄRDIGT.

       Tidigare bockades allt av redan vid insamlingen. Följden blev att artiklar
       som samlats in men aldrig hunnit publiceras försvann tyst för alltid — de
       räknades som sedda trots att de aldrig nått sajten. Anropa därför den här
       först när artikeln är klar (översatt och sparad för publicering).
       """
       if not articles:
           return

       seen_urls = self.load_seen_urls()
       before = len(seen_urls)

       for article in articles:
           # En sammanslagen notis har flera adresser – alla ska bockas av,
           # annars samlas de andra in på nytt i morgon som "nya".
           for url in article.get('alla_urler', []):
               if url:
                   seen_urls.add(url)
           url = article.get('url') or article.get('original_url')
           if url:
               seen_urls.add(url)

       with open("seen_articles.json", "w", encoding='utf-8') as f:
           json.dump(sorted(seen_urls), f, ensure_ascii=False)

       logger.info(f"✅ Bockade av {len(seen_urls) - before} färdigbearbetade artiklar")

   def filter_new_articles(self, articles):
       """Filtrera bort artiklar vi redan hanterat.

       OBS: skriver inte längre till seen_articles.json. Avbockningen sker i
       mark_as_seen(), efter att artikeln bearbetats färdigt.
       """
       seen_urls = self.load_seen_urls()

       new_articles = [a for a in articles if a['url'] not in seen_urls]

       logger.info(f"🔍 Filtrerade till {len(new_articles)} nya artiklar av {len(articles)} totalt")
       return new_articles
   
   def grupp_samma_handelse(self, articles):
       """Hitta artiklar som handlar om exakt samma händelse.

       Flera källor bevakar samma sak: en förhandsartikel om en turnering, ett
       uppmärksammat parti, ett FIDE-beslut. Tidigare blev det tre notiser om
       samma nyhet i flödet. Nu grupperas de, och gruppen blir en enda artikel
       med alla källor angivna.

       Bara TITLARNA skickas till Claude, inte artikeltexterna – det är ett
       billigt anrop och tillräckligt för att avgöra om två rubriker beskriver
       samma händelse. Går anropet fel behandlas varje artikel för sig, precis
       som förut.
       """
       if len(articles) < 2 or not anthropic_client:
           return [[a] for a in articles]

       lista = "\n".join(
           f"{i}. [{a['source']}] {a['title']}" for i, a in enumerate(articles)
       )

       prompt = f"""Nedan är rubriker på schacknyheter från olika källor.

Hitta de rubriker som handlar om EXAKT SAMMA händelse — samma parti, samma
turneringsomgång, samma beslut, samma person i samma sammanhang.

Slå ihop när en läsare skulle uppfatta dem som samma nyhet, även om
rubrikerna är formulerade helt olika och på olika språk. Exempel som SKA
slås ihop:
- "Van Foreest wins Dutch Championship" och "Nederländska mästerskapen avgjorda"
- "Sivanandan stuns Royal" och "11-åring slog stormästaren i British Championship"
- en förhandsartikel om samma turnering från tre olika källor

Exempel som INTE ska slås ihop:
- samma turnering men olika ronder
- samma spelare men olika händelser
- en turnering och en annan turnering med liknande namn

Svara med en rad per grupp som har fler än en artikel, med siffrorna
kommaseparerade. Finns inga sådana grupper, svara med ordet INGA.

Exempel på svar:
0,4
2,7,9

RUBRIKER:
{lista}"""

       try:
           # Rejält tilltaget utrymme. Med max_tokens=300 gick hela svaret åt
           # till modellens tankeblock, och kvar blev ingen text alls —
           # grupperingen föll då tillbaka på att behandla allt var för sig.
           svar = hamta_text(claude_message(
               max_tokens=2000,
               messages=[{"role": "user", "content": prompt}]
           )).strip()
       except Exception as e:
           logger.warning(f"⚠️ Kunde inte gruppera artiklar: {e} – behandlar dem var för sig")
           return [[a] for a in articles]

       grupperade = set()
       grupper = []
       if "INGA" not in svar.upper():
           for rad in svar.splitlines():
               # Bara rader som är rena sifferlistor godtas. Skriver modellen
               # "Grupp 1: 0, 2" skulle ettan i etiketten annars läsas som en
               # artikel och slå ihop fel saker. Hellre ingen sammanslagning
               # än en felaktig.
               if not re.fullmatch(r"[\d,\s]+", rad.strip() or "x"):
                   continue
               siffror = [int(t) for t in re.findall(r"\d+", rad)]
               giltiga = [i for i in siffror if 0 <= i < len(articles) and i not in grupperade]
               if len(giltiga) > 1:
                   grupper.append([articles[i] for i in giltiga])
                   grupperade.update(giltiga)

       for i, a in enumerate(articles):
           if i not in grupperade:
               grupper.append([a])

       ihopslagna = sum(len(g) for g in grupper if len(g) > 1)
       if not ihopslagna:
           # Utan det här går det inte att avgöra om modellen svarade "INGA"
           # eller om svaret var obegripligt och kastades bort.
           logger.info(f"🔗 Inga dubbletter hittades. Claudes svar: {svar[:200]!r}")
       if ihopslagna:
           logger.info(
               f"🔗 {ihopslagna} artiklar handlade om samma händelser "
               f"och blir {len([g for g in grupper if len(g) > 1])} sammanslagna notiser"
           )
       return grupper

   def translate_article_with_claude(self, article):
       """Översätt artikel med Claude"""
       if not anthropic_client:
           logger.warning(f"⚠️ Kan inte översätta {article['title']} - Claude inte tillgänglig")
           return None
       
       try:
           source = next((s for s in self.sources if s.name == article['source']), None)
           if not source:
               return None
           
           content = source.parse_article_content(article['url'])

           # TWIC och andra RSS-källor skickar med en sammanfattning i flödet.
           # Går själva artikelsidan inte att läsa är den bättre än ingenting.
           if (not content or len(content) < 100) and article.get('_rss_content'):
               content = article['_rss_content']
               if len(content) >= 100:
                   logger.info(f"📄 Använder flödestexten för {article['title'][:50]}")

           if not content or len(content) < 100:
               logger.warning(f"⚠️ För kort innehåll från {article['url']}")
               return None
           
           source_language = "engelska"
           if article['source'] == "Europe Echecs":
               source_language = "franska"
           elif article['source'] == "Schack.se":
               source_language = "svenska"
               
           if source_language == "svenska":
               if len(content) > 1200:
                   content = content[:1200] + "..."
                   
               return {
                   "source": article['source'],
                   "original_url": article['url'],
                   "original_title": article['title'],
                   "swedish_title": article['title'],
                   "swedish_content": content,
                   "date": article['date'],
                   "tag": article['tag'],
                   "processed_at": datetime.now().isoformat()
               }

           # Taket är ett tak, inte ett mål. Tidigare stod det "ca N tecken", och
           # då fylldes texten ut med samma fakta i omskrivning tills den nådde
           # dit. En notis med tre fakta ska få vara tre meningar lång.
           max_chars = max(400, min(1600, int(len(content) * 0.45)))
           max_chars = round(max_chars / 50) * 50

           prompt = f"""Du är en schackjournalist som skriver nyhetsnotiser på svenska.

SÅ HÄR SKRIVER DU:
- Kort, konkret rubrik på svenska (max 10 ord)
- Rapportera händelsen direkt. Skriv aldrig "Enligt [källa]" eller "[Källa] rapporterar"
- Behåll alla egennamn, turneringsnamn och förkortningar exakt som i originalet
- Avsluta med ett konkret faktum, resultat eller en konsekvens – aldrig en uppmaning

LÄNGDEN STYRS AV INNEHÅLLET:
- Skriv bara så långt som fakta räcker. Har originalet tre uppgifter blir det tre
  meningar. {max_chars} tecken är ett TAK, inte något att sträva mot.
- Säg varje sak EN gång. Upprepa inte samma resultat, namn eller poängställning
  i omskriven form senare i texten.
- Ta med: vem, vad, var, när, resultat och det som faktiskt är nytt.
- Utelämna: reklam, uppmaningar att prenumerera, upprepade tabellrader,
  självreferenser till källans egen bevakning, och allmänna omdömen utan fakta.

{SCHACKTERMER}

ARTIKELNS TYP — välj exakt en av dessa och skriv den på TYP-raden:
Resultat · Porträtt · Pedagogik · Schackpolitik · Teknik och AI · Historia och kultur · Övrigt
(Resultat = turnerings- och rondrapporter. Porträtt = intervjuer och personporträtt.
Pedagogik = skolschack, träning, undervisning. Schackpolitik = FIDE, förbund, regler,
val och konflikter. Teknik och AI = motorer, fusk, forskning, plattformar.)

FORMAT:
RUBRIK: [din svenska rubrik]
TYP: [en av typerna ovan]
TEXT: [din svenska text]

KÄLLA: {article['source']} ({source_language})
ORIGINALTITEL: {article['title']}
ORIGINALTEXT: {content[:2500]}"""

           response = claude_message(
               max_tokens=1500,
               temperature=0.2,
               messages=[{"role": "user", "content": prompt}]
           )
           
           claude_text = hamta_text(response)
           swedish_title, artikeltyp, swedish_content = dela_upp_svar(claude_text)

           # Säkerhetsspärr mot orimligt långa svar. Tidigare kapades texten rått
           # vid 1000 tecken mitt i ordet ("Tävlingen följer återigen ett ..."),
           # trots att prompten ovan ber om upp till 1400 tecken. Koden förstörde
           # alltså precis det den just beställt. Nu ligger taket över det vi ber
           # om, och kapningen sker alltid vid ett meningsslut.
           tak = max(2000, int(max_chars * 1.4))
           swedish_content = korta_vid_meningsslut(swedish_content, tak)

           result = {
               "source": article['source'],
               "original_url": article['url'],
               "original_title": article['title'],
               "swedish_title": swedish_title,
               "swedish_content": swedish_content,
               "date": article['date'],
               "tag": article['tag'],
               "processed_at": datetime.now().isoformat(),
               "typ": artikeltyp,
           }
           
           logger.info(f"✅ Översatt med Claude ({article['source']}, {source_language}): {swedish_title}")
           return result
           
       except Exception as e:
           logger.error(f"❌ Claude-fel för {article['url']}: {e}")
           return None
   
   def translate_group_with_claude(self, grupp):
       """Skriv EN artikel av flera källors bevakning av samma händelse.

       Källorna kompletterar ofta varandra: den ena har partiet, den andra
       citatet, den tredje ställningen efteråt. Här får Claude allihop och
       skriver en notis, som anger samtliga källor.
       """
       if not anthropic_client:
           return None

       texter = []
       med_innehall = []
       for art in grupp:
           source = next((sr for sr in self.sources if sr.name == art['source']), None)
           if not source:
               continue
           innehall = source.parse_article_content(art['url'])
           if innehall and len(innehall) >= 100:
               med_innehall.append(art)
               texter.append(f"--- KÄLLA: {art['source']} ---\nRUBRIK: {art['title']}\n{innehall[:2000]}")

       if not med_innehall:
           logger.warning("⚠️ Ingen av de sammanslagna artiklarna gick att läsa")
           return None
       if len(med_innehall) == 1:
           # Bara en gick att läsa – då är det ingen sammanslagning längre
           return self.translate_article_with_claude(med_innehall[0])

       samlad = "\n\n".join(texter)
       max_chars = max(500, min(1800, int(len(samlad) * 0.30)))
       max_chars = round(max_chars / 50) * 50

       kallnamn = ", ".join(a['source'] for a in med_innehall)

       prompt = f"""Du är en schackjournalist som skriver nyhetsnotiser på svenska.

Nedan följer {len(med_innehall)} artiklar från olika källor om SAMMA händelse.
Skriv EN notis av dem.

SÅ HÄR SKRIVER DU:
- Kort, konkret rubrik på svenska (max 10 ord)
- Rapportera händelsen direkt. Skriv aldrig "Enligt [källa]" eller "[Källa] rapporterar"
- Nämn inte att det finns flera källor – det står i sidfoten under artikeln
- Behåll alla egennamn, turneringsnamn och förkortningar exakt som i originalen
- Avsluta med ett konkret faktum, resultat eller en konsekvens

NÄR KÄLLORNA ÖVERLAPPAR:
- Skriv varje uppgift EN gång, även om alla källor tar upp den
- Ta med det som bara en källa har, om det tillför något
- Säger källorna emot varandra om en siffra eller ett namn: skriv det som
  flest källor anger, och utelämna det osäkra hellre än att gissa

LÄNGDEN STYRS AV INNEHÅLLET:
- {max_chars} tecken är ett TAK, inte något att sträva mot
- Flera källor betyder inte längre text, bara säkrare fakta

{SCHACKTERMER}

ARTIKELNS TYP — välj exakt en:
Resultat · Porträtt · Pedagogik · Schackpolitik · Teknik och AI · Historia och kultur · Övrigt

FORMAT:
RUBRIK: [din svenska rubrik]
TYP: [en av typerna ovan]
TEXT: [din svenska text]

KÄLLOR: {kallnamn}

{samlad}"""

       try:
           claude_text = hamta_text(claude_message(
               max_tokens=1800,
               temperature=0.2,
               messages=[{"role": "user", "content": prompt}]
           ))
       except Exception as e:
           logger.error(f"❌ Kunde inte slå ihop artiklarna: {e}")
           return None

       swedish_title, artikeltyp, swedish_content = dela_upp_svar(claude_text)

       tak = max(2000, int(max_chars * 1.4))
       if len(swedish_content) > tak:
           swedish_content = korta_vid_meningsslut(swedish_content, tak)

       huvud = med_innehall[0]
       logger.info(f"🔗 Slog ihop {len(med_innehall)} källor: {swedish_title[:60]}")

       return {
           "source": huvud['source'],
           "original_url": huvud['url'],
           "original_title": huvud['title'],
           "swedish_title": swedish_title,
           "swedish_content": swedish_content,
           "date": huvud['date'],
           "tag": huvud['tag'],
           "processed_at": datetime.now().isoformat(),
           "typ": artikeltyp,
           # Alla källor, för sidfoten under artikeln
           "alla_kallor": [{"source": a['source'], "url": a['url']} for a in med_innehall],
           # Alla adresser, så att ingen av dem samlas in på nytt nästa körning
           "alla_urler": [a['url'] for a in med_innehall],
       }

   def process_articles_with_claude(self, articles):
       """Bearbeta artiklar med Claude"""
       processed = []

       grupper = self.grupp_samma_handelse(articles)
       logger.info(f"🤖 Översätter {len(articles)} artiklar i {len(grupper)} notiser...")

       for grupp in grupper:
           if len(grupp) > 1:
               result = self.translate_group_with_claude(grupp)
           else:
               result = self.translate_article_with_claude(grupp[0])
           if result:
               processed.append(result)
           time.sleep(2)

       return processed
   
   def save_for_approval(self, articles):
       """Spara artiklar för godkännande"""
       if not articles:
           return

       filename = "pending_approval.json"

       with open(filename, "w", encoding='utf-8') as f:
           json.dump(articles, f, indent=2, ensure_ascii=False)

       logger.info(f"💾 Sparade {len(articles)} nya artiklar i {filename}")

   def save_as_wp_drafts(self, articles):
       """Lägg artiklarna som utkast direkt i WordPress.

       Tidigare skrevs bara pending_approval.json till repot, och ett
       WordPress-tillägg skulle hämta filen därifrån. Den vägen gick sönder
       tyst: tillägget hämtade filen, såg fyra nya artiklar och importerade
       noll utan att ange varför.

       Utkasten märks med en dold GAMBIT_META-kommentar. Både
       /redaktionen/index.php och tillägget "Gambit Redaktion" filtrerar
       utkast på just den märkningen.

       Returnerar de artiklar som faktiskt nådde WordPress, så att inget
       bockas av i onödan om uppladdningen skulle fallera.
       """
       if not articles:
           return []

       if not all([WP_URL, WP_USER, WP_PASS]):
           logger.warning("⚠️ WordPress-inställningar saknas – faller tillbaka på JSON")
           self.save_for_approval(articles)
           return []

       auth_str = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode()
       headers = {
           "Authorization": f"Basic {auth_str}",
           "Content-Type": "application/json",
           "User-Agent": "Gambit-News/1.0",
       }

       saved = 0
       failed = 0
       sparade_artiklar = []

       # Sortera äldst först efter originalartikelns datum. Kommer flera
       # rondrapporter från samma turnering i klump hamnar de då i rätt inbördes
       # ordning i flödet – rond 3 efter rond 2 – i stället för huller om buller.
       def _datumnyckel(a):
           try:
               return dateparser.parse(a.get("date") or "")
           except Exception:
               return None

       articles = sorted(
           articles,
           key=lambda a: (_datumnyckel(a) is None, _datumnyckel(a) or datetime.min),
       )

       for art in articles:
           cat_slug = CATEGORY_MAPPING.get(art.get("source", ""), "ovrigt")
           meta = {
               "source":         art.get("source", ""),
               "source_url":     art.get("original_url", ""),
               # Har flera källor skrivit om samma sak listas de alla här, så att
               # sidfoten under artikeln kan hänvisa till var och en av dem.
               "alla_kallor":    art.get("alla_kallor", []),
               # Vilken sorts nyhet det är. Visas i redaktionen så att det syns
               # direkt om dagens skörd bara består av turneringsresultat.
               "typ":            art.get("typ", "Övrigt"),
               "suggested_cat":  cat_slug,
               "original_title": art.get("original_title", ""),
               # Originalartikelns datum sparas som eget fält. Det är sant om
               # världen och ändras aldrig. Publiceringsdatumet är en annan sak:
               # det säger när Gambit lade upp texten och styr ordningen i
               # flödet. Tidigare delade de på samma fält, vilket gjorde att
               # nypublicerat kunde landa långt bakåt där ingen ser det.
               "original_date":  art.get("date", ""),
           }
           meta_comment = f"<!-- GAMBIT_META:{json.dumps(meta, ensure_ascii=False)} -->"
           wp_content   = meta_comment + "\n\n" + art.get("swedish_content", "")

           payload = {
               "title":   art.get("swedish_title", art.get("original_title", "Schacknyhet")),
               "content": wp_content,
               "status":  "draft",
               # Inget datum skickas med: WordPress sätter det när utkastet
               # publiceras, alltså när artikeln faktiskt blir läsbar på Gambit.
           }

           try:
               resp = requests.post(
                   f"{WP_URL}/wp-json/wp/v2/posts",
                   headers=headers,
                   json=payload,
                   timeout=20,
               )
               if resp.status_code in (200, 201):
                   saved += 1
                   sparade_artiklar.append(art)
                   post_id = resp.json().get("id", "?")
                   logger.info(f"✅ Utkast #{post_id} skapat: {payload['title'][:60]}")
               else:
                   failed += 1
                   logger.warning(
                       f"⚠️ WP-fel {resp.status_code} för: {payload['title'][:60]} "
                       f"– {resp.text[:200]}"
                   )
           except Exception as e:
               failed += 1
               logger.error(f"❌ Nätverksfel vid WP-utkast: {e}")

           time.sleep(1)

       logger.info(f"💾 WP-utkast: {saved} sparade, {failed} misslyckade")
       return sparade_artiklar

   def run_full_collection(self):
       """Kör fullständig nyhetsinsamling"""
       logger.info("🚀 Startar fullständig nyhetsinsamling med alla förbättringar...")
       
       active_sources = [s.name for s in self.sources if s.enabled]
       logger.info(f"📡 Aktiva källor: {', '.join(active_sources)}")
       
       all_articles = self.collect_from_all_sources()
       logger.info(f"📊 Totalt {len(all_articles)} artiklar från alla källor")
       
       by_source = {}
       for article in all_articles:
           source = article['source']
           by_source[source] = by_source.get(source, 0) + 1
       
       logger.info("📈 Fördelning per källa:")
       for source, count in by_source.items():
           logger.info(f"   {source}: {count} artiklar")
       
       new_articles = self.filter_new_articles(all_articles)
       
       if not new_articles:
           logger.info("📭 Inga nya artiklar hittades")
           return
       
       if anthropic_client:
           processed_articles = self.process_articles_with_claude(new_articles)

           if processed_articles:
               # Lägg utkasten DIREKT i WordPress. Behåll även JSON-filen som
               # säkerhetskopia, men det är WordPress som är sanningen.
               sparade = self.save_as_wp_drafts(processed_articles)
               self.save_for_approval(processed_articles)

               # Bocka av FÖRST nu, och bara det som faktiskt nådde WordPress.
               # Artiklar som fallerat ligger kvar och plockas upp nästa körning
               # i stället för att försvinna tyst.
               self.mark_as_seen(sparade)

               misslyckade = len(new_articles) - len(sparade)
               if misslyckade:
                   logger.warning(
                       f"⚠️ {misslyckade} av {len(new_articles)} artiklar nådde inte WordPress "
                       f"– de ligger kvar och provas igen nästa körning"
                   )

               if not sparade:
                   logger.error(
                       "❌ Ingen artikel nådde WordPress. Kontrollera WP_URL, WP_USER och WP_PASS."
                   )
                   raise RuntimeError("Inga utkast kunde skapas i WordPress")

               logger.info(
                   f"✅ Slutfört! {len(sparade)} utkast redo för granskning på "
                   f"gambit.se/wp-admin/admin.php?page=gambit-redaktion"
               )

               # Skicka e-post automatiskt om artiklar finns
               email_system = EmailApprovalSystem()
               if os.path.exists("pending_approval.json"):
                   email_system.send_approval_email("pending_approval.json")
                   logger.info("📧 E-post skickat automatiskt")

           else:
               # Ingen enda artikel gick igenom. Det är precis det tysta läge som
               # gjorde att sajten stod stilla i månader utan att någon larmade —
               # så nu avslutas körningen med fel, vilket får GitHub Actions att
               # skicka fellarmet.
               logger.error(
                   f"❌ Ingen av {len(new_articles)} artiklar kunde bearbetas. "
                   f"Inget har bockats av. Kontrollera API-nyckel och modellnamn."
               )
               raise RuntimeError("Alla artiklar misslyckades vid bearbetning")
       else:
           logger.warning("⚠️ Claude inte tillgänglig")
           self.save_for_approval(new_articles)

   def send_approval_email_and_start_server(self):
    """Skicka e-post och starta webbserver för godkännande"""
    approval_files = glob.glob("pending_approval_*.json")
    if not approval_files:
        logger.info("📭 Inga artiklar att skicka för godkännande")
        return
    
    latest_file = max(approval_files)
    email_system = EmailApprovalSystem()
    
    # Försök skicka e-post men fortsätt även om det misslyckas
    try:
        if email_system.send_approval_email(latest_file):
            logger.info("📧 E-post skickat framgångsrikt")
        else:
            logger.warning("⚠️ E-post kunde inte skickas")
    except Exception as e:
        logger.warning(f"⚠️ E-post fel: {e}")
        logger.info("📧 Fortsätter ändå med webbserver...")

    # Starta webbserver oavsett e-post-resultat
    logger.info("🌐 Startar webbserver för godkännande...")
    email_system.start_web_server()
    
    print("\n" + "="*70)
    print("🔥 FÖRBÄTTRAT SCHACKNYHETSSYSTEM - REDO FÖR GODKÄNNANDE!")
    print("="*70)
    print("📧 E-post skickat")
    print("🌐 Webbgränssnitt: http://127.0.0.1:5000")
    print("")
    print("🆕 NYA FUNKTIONER:")
    print("✅ Publicera artiklar direkt på gambit.se")
    print("📂 Automatiska WordPress-kategorier per källa")
    print("⏭️ Hoppa över artiklar (försvinner från listan)")
    print("✏️ Redigera rubrik och innehåll före publicering")
    print("🤖 AI-disclaimer läggs till automatiskt")
    print("🎨 Förbättrat webbgränssnitt med snabbtangenter")
    print("🔧 Fixade FIDE och Schack.se källor")
    print("")
    print("⚡ Tryck Ctrl+C för att avsluta servern")
    print("="*70)
    
    # Förbättrad KeyboardInterrupt-hantering
    try:
        while True:
            time.sleep(0.5)  # Kortare sleep för bättre responsivitet
    except KeyboardInterrupt:
        print("\n�� Servern stängd")

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Komplett förbättrat schacknyhetssystem')
    parser.add_argument('--collect', action='store_true', help='Kör fullständig insamling')
    parser.add_argument('--test-sources', action='store_true', help='Testa alla källor')
    parser.add_argument('--test-source', type=str, help='Testa en specifik källa')
    parser.add_argument('--test-wordpress', action='store_true', help='Testa WordPress-anslutning')
    parser.add_argument('--list-sources', action='store_true', help='Lista alla tillgängliga källor')
    parser.add_argument('--approve', action='store_true', help='Skicka e-post och starta godkännandegränssnitt')
    parser.add_argument('--daily', action='store_true', help='Skicka dagligt mejl om ohanterade artiklar')
    args = parser.parse_args()
    engine = MultiNewsEngine()
    if args.collect:
        engine.run_full_collection()
    elif args.test_sources:
        articles = engine.collect_from_all_sources()
        print(f"🎯 Totalt: {len(articles)} artiklar")
        by_source = {}
        for article in articles:
            source = article['source']
            if source not in by_source:
                by_source[source] = []
            by_source[source].append(article)
        for source, arts in by_source.items():
            print(f"📰 {source}: {len(arts)} artiklar")
            if arts:
                print(f"   Exempel: {arts[0]['title'][:60]}...")
    elif args.test_source:
        engine.test_single_source(args.test_source)
    elif args.test_wordpress:
        engine.test_wordpress_connection()
    elif args.list_sources:
        print("📋 Tillgängliga källor:")
        for source in engine.sources:
            status = "✅" if source.enabled else "❌"
            print(f"   {status} {source.name} ({source.base_url})")
    elif args.approve:
        engine.send_approval_email_and_start_server()
    elif args.daily:
        approval_files = glob.glob("pending_approval_*.json")
        if not approval_files:
            logger.info("📭 Inga ohanterade artiklar att mejla om idag.")
            return
        latest_file = max(approval_files)
        with open(latest_file, 'r', encoding='utf-8') as f:
            articles = json.load(f)
        if not articles:
            logger.info("📭 Inga ohanterade artiklar att mejla om idag.")
            return
        email_system = EmailApprovalSystem()
        if email_system.send_approval_email(latest_file):
            logger.info(f"📧 Dagligt mejl skickat med {len(articles)} ohanterade artiklar.")
        else:
            logger.error("❌ Kunde inte skicka dagligt mejl om ohanterade artiklar.")
    else:
        print("🚀 Komplett förbättrat schacknyhetssystem")
        print("\n🆕 Alla förbättringar implementerade:")
        print("  • ✅ Fixade FIDE och Schack.se källor")
        print("  • 📂 Automatiska WordPress-kategorier per källa")
        print("  • ⏭️ 'Hoppa över'-funktion i webbgränssnittet")
        print("  • 🎨 Förbättrat webbgränssnitt med snabbtangenter")
        print("  • 🤖 AI-disclaimer på alla publicerade artiklar")
        print("  • 🔧 Robust felhantering och logging")
        print("  • 📧 Automatisk e-post vid insamling")
        print("\nTillgängliga kommandon:")
        print("  --collect              Kör fullständig insamling")
        print("  --test-sources         Testa alla källor")
        print("  --test-source <namn>   Testa en specifik källa")
        print("  --test-wordpress       Testa WordPress-anslutning")
        print("  --list-sources         Lista alla tillgängliga källor")
        print("  --approve              Skicka e-post och starta godkännandegränssnitt")
        print("  --daily                Skicka dagligt mejl om ohanterade artiklar")
        print("\n💡 Tips: Kör först --test-sources för att se att alla källor fungerar")

if __name__ == "__main__":
    main()
