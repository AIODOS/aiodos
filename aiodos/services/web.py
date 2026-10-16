"""Web public en lecture seule : recherche et lecture de pages.

Sens unique : la seule donnée qui sort est la requête de recherche, validée
par l'utilisateur avant l'envoi. Pas de dépendance : html.parser de la stdlib.
"""
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import requests

# En-tête en ASCII pur : requests encode les en-têtes en latin-1 (le « œ » casse).
HEADERS = {"User-Agent": "Mozilla/5.0 (AIODOS - recherche locale)"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "nav", "footer"}
    BREAK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts, self.title = [], ""
        self._skip, self._in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:                 # le titre est dans <head>, ignoré sinon
            self.title += data
        if self._skip:
            return
        if data.strip():
            self.parts.append(data.strip() + " ")

    def text(self):
        s = re.sub(r"\n\s*\n+", "\n\n", "".join(self.parts))
        return re.sub(r"[ \t]+", " ", s).strip()


def html_to_text(markup: str):
    """(titre, texte) d'un document HTML."""
    parser = _TextExtractor()
    parser.feed(markup)
    return parser.title.strip(), parser.text()


def host_of(url: str) -> str:
    return urlparse(url).netloc


def fetch_url_text(url, max_chars=20000):
    """(titre, texte) d'une page web, ou du texte brut si ce n'est pas du HTML."""
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    body = r.text
    if "html" in r.headers.get("Content-Type", "").lower() or "<html" in body[:1500].lower():
        title, text = html_to_text(body)
        return title or url, text[:max_chars]
    return url, body[:max_chars]


def web_search(query, max_results=3):
    """Recherche publique via DuckDuckGo (version HTML). → [{title, url}]"""
    r = requests.post("https://html.duckduckgo.com/html/",
                      data={"q": query}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                         r.text, re.S):
        href = m.group(1)
        if "y.js" in href or "ad_domain=" in href:      # publicité DuckDuckGo
            continue
        if "uddg=" in href:                              # lien de redirection
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com" in host_of(href):
            continue
        title = re.sub("<[^>]+>", "", m.group(2)).strip()
        results.append({"title": title, "url": href})
        if len(results) >= max_results:
            break
    return results
