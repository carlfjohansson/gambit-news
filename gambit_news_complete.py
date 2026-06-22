# gambit_news_complete.py - Komplett förbättrat schacknyhetssystem

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
import xml.etree.ElementTree as ET

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

# WordPress kategorimappning
CATEGORY_MAPPING = {
    'Chess.com': 'chess-com',
    'ChessBase': 'chessbase', 
    'ChessBase India': 'chessbase-india',
    'FIDE': 'fide',
    'Schack.se': 'schack.se',
    'Chessdom': 'chessdom',
    'Europe Echecs': 'europe-echecs'
}

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
                    return None

            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue

        self.blocked_requests += 1
        return None

    def log_statistics(self):
        if self.total_requests > 0:
            success_rate = (self.successful_requests / self.total_requests) * 100
            avg_response_time = sum(self.response_times) / len(self.response_times) if self.response_times else 0
            logger.info(f"📊 {self.name}: {self.successful_requests}/{self.total_requests} OK ({success_rate:.1f}%), avg {avg_response_time:.2f}s")

    def extract_publish_date(self, article_url, article_html=None):
        """Extrahera publiceringsdatum från artikel"""
        try:
            # För Schack.se: först försök hämta från artikelsidan
            if 'schack.se' in article_url:
                # Om vi har HTML-innehåll, leta efter publiceringsdatum
                if article_html:
                    soup = BeautifulSoup(article_html, "html.parser")
                    
                    # Leta efter "Publicerad XX juli, 2025" text
                    import re
                    text = soup.get_text()
                    date_match = re.search(r'Publicerad (\d{1,2}) (\w+), (\d{4})', text)
                    
                    if date_match:
                        day, month_name, year = date_match.groups()
                        
                        # Översätt svenska månader till siffror
                        month_map = {
                            'januari': '01', 'februari': '02', 'mars': '03', 'april': '04',
                            'maj': '05', 'juni': '06', 'juli': '07', 'augusti': '08',
                            'september': '09', 'oktober': '10', 'november': '11', 'december': '12'
                        }
                        
                        month_num = month_map.get(month_name.lower(), '01')
                        return f"{year}-{month_num}-{day.zfill(2)}T12:00:00"
                
                # Fallback: extrahera från URL
                import re
                match = re.search(r'/nyhet/\w+/(\d{4})/(\d{2})/', article_url)
                if match:
                    year, month = match.groups()
                    return f"{year}-{month}-15T12:00:00"  # Mitten av månaden
            
            # För ChessBase India: försök hitta datum i HTML
            if 'chessbase.in' in article_url and article_html:
                soup = BeautifulSoup(article_html, "html.parser")
                date_selectors = ['time[datetime]', '[class*="date"]', '[class*="published"]']
                
                for selector in date_selectors:
                    element = soup.select_one(selector)
                    if element:
                        date_text = element.get('datetime') or element.get_text()
                        if date_text:
                            try:
                                from dateutil import parser as dateparser
                                parsed_date = dateparser.parse(date_text)
                                if parsed_date:
                                    return parsed_date.isoformat()
                            except:
                                continue
                
        except Exception as e:
            logger.debug(f"Datumextraktion misslyckades för {article_url}: {e}")
        
        # Fallback till igår
        return (datetime.now() - timedelta(days=1)).isoformat()

    @abstractmethod
    def fetch_articles(self):
        pass

    @abstractmethod
    def parse_article_content(self, article_url):
        pass

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
        super().__init__("ChessBase", "https://en.chessbase.com/", "ChessBase", True)
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
                if href and '/post/' in href:
                    if not href.startswith('http'):
                        url = 'https://en.chessbase.com' + href
                    else:
                        url = href

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = link.get_text(strip=True)
                    if not title or len(title) < 5:
                        if link.parent:
                            title = link.parent.get_text(strip=True)
                        if not title:
                            url_parts = href.split('/')
                            if url_parts:
                                title = url_parts[-1].replace('-', ' ').title()

                    if title and len(title) > 10 and len(title) < 300:
                        date = (datetime.now() - timedelta(days=1)).isoformat()
                        articles.append({
                            "source": self.name,
                            "url": url,
                            "title": title,
                            "date": date,
                            "tag": self.tag_name
                        })

                        if len(articles) >= 10:
                            break

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
            '.cb-article-content',
            '.newsText',
            '.article-content',
            '.content'
        ]

        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                return content_element.get_text(strip=True, separator="\n")
        return None

# === FÖRBÄTTRAD FIDE KÄLLA ===
class FideSource(NewsSource):
    def __init__(self):
        super().__init__("FIDE", "https://www.fide.com/news", "FIDE", True)
        self.request_delay = 6

    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []

        try:
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

                potential_links = soup.find_all('a', href=True)
                seen_urls = set()

                for link in potential_links:
                    href = link.get('href', '')
                    text = link.get_text(strip=True)

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
                    break

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

        body = soup.find('body')
        if body:
            content = body.get_text(strip=True, separator="\n")
            if len(content) > 200:
                return content[:1500]

        return None
    
    # === FÖRBÄTTRAD SCHACK.SE KÄLLA ===  
class SchackSeSource(NewsSource):
    def __init__(self):
        super().__init__("Schack.se", "https://schack.se/nyheter", "Sveriges Schackförbund", True)
        self.request_delay = 4

    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            # Hämta huvudsidan först
            resp = self.safe_request_with_backoff("https://schack.se/nyheter/")
            if not resp:
                return articles
                
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Sök efter artiklar med rätt CSS-selektorer
            article_elements = soup.select('article.news-item, div.news-item, div.article-item, div.post')
            
            for element in article_elements:
                link = element.find('a', href=True)
                if link and '/nyhet/' in link.get('href', ''):
                    url = link.get('href')
                    if not url.startswith('http'):
                        url = 'https://schack.se' + url
                    
                    # Extrahera titel från h2/h3 eller link-text
                    title_elem = element.find(['h2', 'h3']) or link
                    title = title_elem.get_text(strip=True)
                    
                    if title and len(title) > 20:
                        # Hämta artikelinnehåll för exakt datumextraktion
                        article_resp = self.safe_request_with_backoff(url)
                        publish_date = self.extract_publish_date(url, article_resp.text if article_resp else None)
                        
                        articles.append({
                            "source": self.name,
                            "url": url,
                            "title": title,
                            "date": publish_date,
                            "tag": self.tag_name
                        })
                        
            if len(articles) == 0:
                # Fallback: sök alla länkar
                all_links = soup.find_all('a', href=lambda x: x and '/nyhet/' in x)
                seen_urls = set()
                
                for link in all_links[:20]:
                    url = link.get('href')
                    if not url.startswith('http'):
                        url = 'https://schack.se' + url
                        
                    if url not in seen_urls:
                        seen_urls.add(url)
                        title = link.get_text(strip=True)
                        
                        if title and len(title) > 20:
                            articles.append({
                                "source": self.name,
                                "url": url,
                                "title": title,
                                "date": self.extract_publish_date(url),
                                "tag": self.tag_name
                            })
                            
        except Exception as e:
            logger.error(f"❌ Fel vid hämtning från {self.name}: {e}")
            self.blocked_requests += 1
        
        self.log_statistics()
        logger.info(f"📰 {self.name}: Extraherade {len(articles)} artiklar")
        return articles[:12]  # Max 12 artiklar

    def parse_article_content(self, article_url):
        # Schack.se använder JavaScript för artikeltext
        return f"Läs hela artikeln på {article_url}"


# === CHESSBASE INDIA KÄLLA ===
class ChessBaseIndiaSource(NewsSource):
    def __init__(self):
        super().__init__("ChessBase India", "https://www.chessbase.in", "ChessBase India", True)
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
                    if not href.startswith('http'):
                        url = 'https://www.chessbase.in' + href
                    else:
                        url = href

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = link.get_text(strip=True)
                    if not title or len(title) < 10:
                        if link.parent:
                            title = link.parent.get_text(strip=True)

                    if title and len(title) > 15 and len(title) < 200:
                        article_resp = self.safe_request_with_backoff(url)
                        publish_date = self.extract_publish_date(url, article_resp.text if article_resp else None)

                        articles.append({
                            "source": self.name,
                            "url": url,
                            "title": title,
                            "date": publish_date,
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
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        
        content_selectors = [
            '.article-content',
            '.post-content',
            '.content'
        ]

        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                return content_element.get_text(strip=True, separator="\n")
        return None


# === CHESSDOM KÄLLA ===
class ChessdomSource(NewsSource):
    def __init__(self):
        super().__init__("Chessdom", "https://www.chessdom.com/", "Chessdom", False)
        self.request_delay = 6

    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []

        try:
            resp = self.safe_request_with_backoff(self.base_url)
            if not resp:
                return articles

            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Sök efter artiklar med olika selektorer
            article_links = soup.select('h2 a[href], h3 a[href], .entry-title a[href]')
            
            seen_urls = set()
            for link in article_links:
                href = link.get('href')
                if href and 'chessdom.com' in href:
                    url = href if href.startswith('http') else 'https://www.chessdom.com' + href
                    
                    # Filtrera bort icke-artiklar
                    if any(skip in url for skip in ['/category/', '/tag/', '/author/', '/page/']):
                        continue
                        
                    if url not in seen_urls and len(url) > 30:
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
        resp = self.safe_request_with_backoff(article_url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        content_selectors = [
            '.entry-content',
            '.article-content',
            '.post-content',
            '.content'
        ]

        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                return content_element.get_text(strip=True, separator="\n")
        return None
    
# === FÖRBÄTTRAD EUROPE ECHECS KÄLLA ===
class EuropeEchecsSource(NewsSource):
    def __init__(self):
        super().__init__("Europe Echecs", "https://www.europe-echecs.com/", "Europe Echecs", True)
        self.request_delay = 5

    def fetch_articles(self):
        logger.info(f"🌍 Hämtar artiklar från {self.name}...")
        articles = []
        
        try:
            # Testa direkt HTML-parsing av huvudsidan
            resp = self.safe_request_with_backoff("https://www.europe-echecs.com/")
            if not resp:
                return articles
                
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Sök efter artiklar med olika selektorer
            article_links = soup.select('a[href*="art"][href$=".html"]')
            
            seen_urls = set()
            for link in article_links:
                href = link.get('href', '')
                if href and not href.startswith('#'):
                    if not href.startswith('http'):
                        url = 'https://www.europe-echecs.com/' + href.lstrip('/')
                    else:
                        url = href
                        
                    if url not in seen_urls and 'art' in url:
                        seen_urls.add(url)
                        
                        title = link.get_text(strip=True)
                        # Om ingen titel, försök hitta i parent
                        if not title or len(title) < 10:
                            parent = link.find_parent(['h2', 'h3', 'div'])
                            if parent:
                                title = parent.get_text(strip=True)
                        
                        if title and len(title) > 15:
                            articles.append({
                                "source": self.name,
                                "url": url,
                                "title": title,
                                "date": (datetime.now() - timedelta(days=1)).isoformat(),
                                "tag": self.tag_name
                            })
                            
                            if len(articles) >= 10:
                                break
                                
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
            'article p',
            '.article-content',
            '.news-content',
            '.post-content',
            '.content'
        ]

        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                content = content_element.get_text(strip=True, separator="\n")
                if len(content) > 100:
                    return content
        return None

# === BESLUTLOGGNING ===
class DecisionLogger:
    def __init__(self):
        self.log_file = "article_decisions.json"
        
    def load_decisions(self):
        """Ladda befintliga beslut"""
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return []
            
    def log_decision(self, article, decision, timestamp=None):
        """Logga ett beslut om en artikel"""
        decisions = self.load_decisions()
        
        decision_entry = {
            "timestamp": timestamp or datetime.now().isoformat(),
            "source": article.get('source'),
            "original_url": article.get('original_url'),
            "title": article.get('swedish_title', article.get('original_title')),
            "decision": decision,  # "published", "skipped", "postponed"
            "wp_url": article.get('wp_url') if decision == "published" else None
        }
        
        decisions.append(decision_entry)
        
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump(decisions, f, indent=2, ensure_ascii=False)
            
    def get_statistics(self):
        """Hämta statistik över beslut"""
        decisions = self.load_decisions()
        stats = {
            "total": len(decisions),
            "published": len([d for d in decisions if d['decision'] == 'published']),
            "skipped": len([d for d in decisions if d['decision'] == 'skipped']),
            "postponed": len([d for d in decisions if d['decision'] == 'postponed'])
        }
        return stats

# === WORDPRESS PUBLISHER MED KATEGORIER OCH DATUM ===
class WordPressPublisher:
    def __init__(self):
        self.wp_url = WP_URL
        self.wp_user = WP_USER  
        self.wp_pass = WP_PASS

    def get_category_id(self, source_name):
        """Skapa eller hämta kategori-ID baserat på källa"""
        try:
            category_slug = CATEGORY_MAPPING.get(source_name, 'allmant')

            categories_url = f"{self.wp_url}/wp-json/wp/v2/categories"
            response = requests.get(categories_url, auth=HTTPBasicAuth(self.wp_user, self.wp_pass))

            if response.status_code == 200:
                categories = response.json()

                for cat in categories:
                    if cat['slug'] == category_slug:
                        logger.info(f"✅ Hittade befintlig kategori: {source_name} (ID: {cat['id']})")
                        return cat['id']

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
            return 1

        except Exception as e:
            logger.warning(f"⚠️ Kunde inte hantera kategori för {source_name}: {e}")
            return 1

    def publish_article(self, selected_article, original_article):
        """Publicera artikel på WordPress med rätt kategori, datum och AI-disclaimer"""
        if not all([self.wp_url, self.wp_user, self.wp_pass]):
            logger.warning("⚠️ WordPress-inställningar saknas")
            return False

        try:
            category_id = self.get_category_id(original_article['source'])
            api_url = f"{self.wp_url}/wp-json/wp/v2/posts"

            # Använd originaldatum om tillgängligt
            publish_date = original_article.get('date')
            if publish_date:
                try:
                    # Konvertera till WordPress-format
                    dt = dateparser.parse(publish_date)
                    wp_date = dt.strftime('%Y-%m-%dT%H:%M:%S')
                except:
                    import re
                    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', str(publish_date))
                    if date_match:
                        wp_date = date_match.group(1) + 'T00:00:00'
                    else:
                        wp_date = publish_date

            formatted_content = f"""
{selected_article['content']}

<hr style="margin: 20px 0; border: none; height: 1px; background: #ddd;">

<div style="background: #f9f9f9; padding: 15px; border-left: 4px solid #0073aa; margin: 15px 0;">
<p style="margin: 0; font-style: italic; color: #666;">

<strong>Publicerad:</strong> {publish_date.split('T')[0] if publish_date else 'Okänt datum'}<br>
<strong>Källa:</strong> {original_article['source']}<br>
<a href="{original_article['original_url']}" target="_blank" rel="noopener">Originalartikeln</a> är bearbetad med hjälp av claude.ai
</div>
"""

            post_data = {
                'title': selected_article['title'],
                'content': formatted_content,
                'status': 'publish',
                'categories': [category_id],
                'excerpt': selected_article['content'][:150] + '...',
                'meta': {
                    'source_url': original_article['original_url'],
                    'source_name': original_article['source'],
                    'ai_translated': True
                }
            }
            
            # Lägg till datum om det finns
            if wp_date:
                post_data['date'] = wp_date
                post_data['date_gmt'] = wp_date

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
                logger.info(f"   📅 Datum: {publish_date.split('T')[0] if publish_date else 'Idag'}")
                logger.info(f"   🔗 URL: {post_url}")
                
                # Lägg till WordPress URL till original_article för loggning
                original_article['wp_url'] = post_url
                return True
            else:
                logger.error(f"❌ WordPress-fel: {response.status_code} - {response.text}")
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
        """Hantera både publicering och borttagning av artiklar med loggning"""
        try:
            data = request.get_json()
            to_publish = data.get('publish', [])
            to_skip = data.get('skip', [])
            
            decision_logger = DecisionLogger()

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
                            decision_logger.log_decision(original_article, "published")
                            logger.info(f"✅ Publicerade: {selected['title']}")
                        else:
                            logger.error(f"❌ Kunde inte publicera: {selected['title']}")
            elif to_publish:
                logger.warning("⚠️ WordPress inte konfigurerat - kan inte publicera artiklar")

            # Logga överhoppade artiklar
            for skipped in to_skip:
                if skipped['id'] < len(all_articles):
                    original_article = all_articles[skipped['id']]
                    decision_logger.log_decision(original_article, "skipped")

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
            msg['Subject'] = f"🔥 {article_count} nya schackartiklar väntar på godkännande"

            body = f"""
Hej!

{article_count} nya schackartiklar har samlats in och översatts och väntar på ditt godkännande.

📊 Fördelning per källa:
"""

            by_source = {}
            for article in articles:
                source = article['source']
                by_source[source] = by_source.get(source, 0) + 1

            for source, count in by_source.items():
                body += f"   • {source}: {count} artiklar\n"

            body += f"""

🔗 För att granska artiklarna:

1. Kör detta kommando i terminalen:
   python gambit_news_complete.py --approve

2. Eller klicka här när servern är igång:
   http://127.0.0.1:5000

📁 Artiklar sparade i: {articles_file}

🆕 Nya funktioner:
✅ Publicera artiklar direkt på gambit.se med automatiska kategorier
⏭️ Hoppa över artiklar (de försvinner från listan)
✏️ Redigera rubrik och innehåll före publicering
📂 Automatiska WordPress-kategorier per källa
🤖 AI-disclaimer läggs till automatiskt

/Ditt automatiska schacknyhetssystem
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
            self.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)

        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()

        threading.Timer(2.0, lambda: webbrowser.open('http://127.0.0.1:5000')).start()

        logger.info("🌐 Webbserver startad på http://127.0.0.1:5000")

        # === HUVUDMOTOR MED ALLA FÖRBÄTTRINGAR ===
class MultiNewsEngine:
    def __init__(self):
        self.sources = [
            ChesscomSource(),
            ChessBaseSource(),          
            FideSource(),
            SchackSeSource(),
            ChessBaseIndiaSource(),
            ChessdomSource(),
            EuropeEchecsSource()
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

    def filter_new_articles(self, articles):
        """Filtrera bort artiklar vi redan sett"""
        try:
            with open("seen_articles.json", "r", encoding='utf-8') as f:
                seen_urls = set(json.load(f))
        except FileNotFoundError:
            seen_urls = set()

        new_articles = []
        for article in articles:
            if article['url'] not in seen_urls:
                new_articles.append(article)

        all_urls = seen_urls | {art['url'] for art in articles}
        with open("seen_articles.json", "w", encoding='utf-8') as f:
            json.dump(list(all_urls), f)

        logger.info(f"🔍 Filtrerade till {len(new_articles)} nya artiklar av {len(articles)} totalt")
        return new_articles

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
            if not content or len(content) < 20:
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

            if source_language == "svenska":
                # Bearbeta även svenska artiklar för att korta ner dem
                prompt = f"""Du är expert på svenska schacknyheter och ska förkorta/förbättra denna svenska text.

INSTRUKTIONER:
- Skriv en kort, engagerande rubrik (max 10 ord)
- Förkorta texten till max 600-800 tecken
- Ta bort onödig information och fyllnadstext
- Ignorera alla hänvisningar till bilder, foton eller diagram
- Inkludera "rapporterar Schack.se" eller liknande NATURLIGT i texten
- Behåll viktiga fakta och resultat
- Avsluta med länk till originalkällan

FORMAT:
RUBRIK: [din svenska rubrik här]
TEXT: [din korta svenska text här - max 800 tecken]

Läs mer på originalartikeln: {article['url']}

ORIGINALTEXT: {content[:2500]}
ORIGINALTITEL: {article['title']}"""

            else:
                prompt = f"""Du är en expert på svenska schacknyheter och översätter från {source_language} till svenska.

VIKTIGA INSTRUKTIONER:
- Skriv en kort, engagerande svensk rubrik (max 10 ord)
- Inkludera källan NATURLIGT i texten, t.ex: "skriver {article['source']}", "rapporterar {article['source']}", "enligt {article['source']}"
- Behåll ALLA egennamn EXAKT som i originalet
- Behåll ALLA förkortningar EXAKT som i originalet  
- Använd etablerade svenska schacktermer
- GÖR TEXTEN KORTARE - max 600-800 tecken
- Fokusera på viktigaste fakta och resultat
- Ta bort onödig fyllnadstext
- Ignorera alla hänvisningar till bilder, foton eller diagram
- Avsluta med länk till originalkällan

FORMAT:
RUBRIK: [din svenska rubrik här]
TEXT: [din korta svenska text här med källa inbyggd - max 800 tecken]

Läs mer på originalartikeln: {article['url']}

KÄLLA: {article['source']} ({source_language})
ORIGINALTITEL: {article['title']}
ORIGINALTEXT: {content[:2500]}"""

            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}]
            )

            claude_text = response.content[0].text.strip()

            if "RUBRIK:" in claude_text and "TEXT:" in claude_text:
                parts = claude_text.split("TEXT:", 1)
                swedish_title = parts[0].replace("RUBRIK:", "").strip()
                swedish_content = parts[1].strip()
            else:
                lines = claude_text.split("\n", 1)
                swedish_title = lines[0].strip()
                swedish_content = lines[1].strip() if len(lines) > 1 else ""

            if len(swedish_content) > 1000:
                swedish_content = swedish_content[:1000] + "..."

            result = {
                "source": article['source'],
                "original_url": article['url'],
                "original_title": article['title'],
                "swedish_title": swedish_title,
                "swedish_content": swedish_content,
                "date": article['date'],
                "tag": article['tag'],
                "processed_at": datetime.now().isoformat()
            }

            logger.info(f"✅ Översatt med Claude ({article['source']}, {source_language}): {swedish_title}")
            return result

        except Exception as e:
            logger.error(f"❌ Claude-fel för {article['url']}: {e}")
            return None

    def process_articles_with_claude(self, articles):
        """Bearbeta artiklar med Claude"""
        processed = []

        logger.info(f"🤖 Översätter {len(articles)} artiklar med Claude...")

        for article in articles:
            result = self.translate_article_with_claude(article)
            if result:
                processed.append(result)
            time.sleep(2)

        return processed

    def save_for_approval(self, articles):
        """Spara artiklar för godkännande"""
        today = datetime.now().strftime('%Y%m%d')
        filename = f"pending_approval_{today}.json"
    
        existing = []
        try:
            with open(filename, "r", encoding='utf-8') as f:
                existing = json.load(f)
        except FileNotFoundError:
            pass
    
        all_articles = existing + articles
    
        with open(filename, "w", encoding='utf-8') as f:
            json.dump(all_articles, f, indent=2, ensure_ascii=False)
    
        logger.info(f"💾 Sparade {len(articles)} nya artiklar i {filename}")

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
                self.save_for_approval(processed_articles)
                logger.info(f"✅ Slutfört! {len(processed_articles)} artiklar redo för godkännande")

                # Skicka e-post automatiskt om artiklar finns
                email_system = EmailApprovalSystem()
                today = datetime.now().strftime('%Y%m%d')
                filename = f"pending_approval_{today}.json"

                if os.path.exists(filename):
                    email_system.send_approval_email(filename)
                    logger.info("📧 E-post skickat automatiskt")

            else:
                logger.warning("⚠️ Inga artiklar kunde översättas")
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
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n👋 Servern stängd")

    def test_wordpress_connection(self):
        """Testa WordPress-anslutning och kategorier"""
        if not all([WP_URL, WP_USER, WP_PASS]):
            logger.error("❌ WordPress-inställningar saknas i .env")
            logger.info("Lägg till följande i din .env-fil:")
            logger.info("WP_URL=https://din-wordpress-site.com")
            logger.info("WP_USER=ditt-användarnamn")
            logger.info("WP_PASS=ditt-lösenord")
            return False

        logger.info("🧪 Testar WordPress-anslutning...")
        wp_publisher = WordPressPublisher()

        try:
            categories_url = f"{WP_URL}/wp-json/wp/v2/categories"
            response = requests.get(categories_url, auth=HTTPBasicAuth(WP_USER, WP_PASS))

            if response.status_code == 200:
                logger.info("✅ WordPress-anslutning fungerar")
                categories = response.json()
                logger.info(f"📂 Hittade {len(categories)} befintliga kategorier")

                logger.info("🔧 Testar kategori-skapning...")
                for source in self.sources:
                    category_id = wp_publisher.get_category_id(source.name)
                    logger.info(f"   {source.name}: Kategori-ID {category_id}")

                return True
            else:
                logger.error(f"❌ WordPress-fel: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ Kunde inte ansluta till WordPress: {e}")
            return False

    def test_single_source(self, source_name):
        """Testa en enskild källa"""
        source = next((s for s in self.sources if s.name.lower() == source_name.lower()), None)
        if not source:
            logger.error(f"❌ Källa '{source_name}' hittades inte")
            available = [s.name for s in self.sources]
            logger.info(f"📋 Tillgängliga källor: {', '.join(available)}")
            return

        logger.info(f"🧪 Testar källa: {source.name}")
        articles = source.fetch_articles()

        if articles:
            logger.info(f"✅ {source.name}: Hittade {len(articles)} artiklar")
            for i, article in enumerate(articles[:3], 1):
                logger.info(f"   {i}. {article['title'][:80]}...")
            
            logger.info(f"🔍 Testar parsing av första artikeln:")
            test_article = articles[0]
            logger.info(f"🔗 URL: {test_article['url']}")
            content = source.parse_article_content(test_article['url'])
            if content:
                logger.info(f"✅ Parsing lyckades: {len(content)} tecken")
                logger.info(f"📄 Första 200 tecken: {content[:200]}...")
            else:
                logger.info("❌ Parsing misslyckades - inget innehåll hittat")
        else:
            logger.warning(f"⚠️ {source.name}: Inga artiklar hittades")

        source.log_statistics()

# === MAIN ===
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
    parser.add_argument('--stats', action='store_true', help='Visa statistik över artikelbeslut')

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
    elif args.stats:
        decision_logger = DecisionLogger()
        stats = decision_logger.get_statistics()
        print("\n📊 STATISTIK ÖVER ARTIKELBESLUT")
        print("="*40)
        print(f"📰 Totalt behandlade: {stats['total']}")
        print(f"✅ Publicerade: {stats['published']}")
        print(f"⏭️ Överhoppade: {stats['skipped']}")
        print(f"⏸️ Uppskjutna: {stats['postponed']}")
        print("="*40)
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
        print("  • 📊 Loggning av alla artikelbeslut")
        print("\nTillgängliga kommandon:")
        print("  --collect              Kör fullständig insamling")
        print("  --test-sources         Testa alla källor")
        print("  --test-source <namn>   Testa en specifik källa")
        print("  --test-wordpress       Testa WordPress-anslutning")
        print("  --list-sources         Lista alla tillgängliga källor")
        print("  --approve              Skicka e-post och starta godkännandegränssnitt")
        print("  --daily                Skicka dagligt mejl om ohanterade artiklar")
        print("  --stats                Visa statistik över artikelbeslut")
        print("\n💡 Tips: Kör först --test-sources för att se att alla källor fungerar")

if __name__ == "__main__":
    main()

    if __name__ == "__main__":
        print("🚀 Startar programmet...")  # Lägg till denna rad för debugging
        main()
