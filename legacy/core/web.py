"""Accès web public (recherche + lecture de pages) — pour la recherche cloud.

Sens unique : on ne fait que LIRE des pages publiques. La seule donnée qui sort
est la requête de recherche (validée par l'utilisateur avant envoi).
Aucune dépendance externe : extraction du texte via html.parser de la stdlib.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urlparse, unquote, parse_qs

import requests

# NB : en-tête en ASCII pur — requests encode les headers en latin-1 (le « œ » casse).
HEADERS = {"User-Agent": "Mozilla/5.0 (Coeur IA - recherche locale)"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "nav", "footer"}
    BREAK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip > 0:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:                 # le titre est dans <head> (ignoré sinon)
            self.title += data
        if self._skip:
            return
        t = data.strip()
        if t:
            self.parts.append(t + " ")

    def text(self):
        s = "".join(self.parts)
        s = re.sub(r"\n\s*\n+", "\n\n", s)
        s = re.sub(r"[ \t]+", " ", s)
        return s.strip()


def host_of(url):
    return urlparse(url).netloc


def fetch_url_text(url, max_chars=20000):
    """Retourne (titre, texte) d'une page web (ou texte brut)."""
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    body = r.text
    if "html" in ctype or "<html" in body[:1500].lower():
        p = _TextExtractor()
        p.feed(body)
        return (p.title.strip() or url), p.text()[:max_chars]
    return url, body[:max_chars]


def web_search(query, max_results=3):
    """Recherche publique via DuckDuckGo (HTML). Retourne [{title, url}]."""
    r = requests.post("https://html.duckduckgo.com/html/",
                      data={"q": query}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    results = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S):
        href = m.group(1)
        title = re.sub("<[^>]+>", "", m.group(2)).strip()
        if "y.js" in href or "ad_domain=" in href:   # pub DuckDuckGo : on ignore
            continue
        if "uddg=" in href:                           # lien de redirection DDG
            qs = parse_qs(urlparse(href).query)
            href = unquote(qs.get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com" in urlparse(href).netloc:  # liens internes DDG
            continue
        results.append({"title": title, "url": href})
        if len(results) >= max_results:
            break
    return results
