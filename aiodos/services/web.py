"""Web public en lecture seule : recherche et lecture de pages.

Sens unique : la seule donnée qui sort est la requête de recherche, validée
par l'utilisateur avant l'envoi. Pas de dépendance : html.parser de la stdlib.

Une lecture de page est bornée (audit du 20/10/2026) :
- adresses publiques seulement, sauf pour une page du réseau local demandée sur cette
  machine (public_only=False, cf. services/addresses.py) ; chaque redirection est suivie à
  la main et revérifiée ;
- corps lu en flux, MAX_BYTES au plus (octets décompressés : une page compressée qui
  gonfle ne remplit pas la mémoire), types binaires ignorés sans être lus ; le jeu de
  caractères annoncé n'est suivi que s'il est de ceux du Web (WEB_CHARSETS : zlib,
  base64, punycode… n'en sont pas, ils rendraient des octets, décompresseraient, ou
  décoderaient pendant des heures après l'échéance) ;
- durée totale bornée (TOTAL_TIMEOUT) : le délai de requests ne vaut qu'entre deux
  lectures, une page qui distille ses octets le contournerait ; un fil de garde coupe la
  connexion au délai, ou dès que l'appelant demande l'arrêt (stop), et la résolution du
  nom comme chaque essai de connexion s'arrêtent à la même échéance (addresses.Guard).
La recherche (web_search) l'est de la même façon (relecture du 21/10/2026) : taille
(SEARCH_MAX_BYTES), durée totale (SEARCH_TIMEOUT), arrêt sur demande, ni proxy ni .netrc ;
et seuls ses résultats en http(s) sont rendus.
Les erreurs sont dites en clair (PageUnreadable), sans le détail du réseau.
"""
import codecs
import re
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from aiodos.i18n import t
from aiodos.services import addresses

# En-tête en ASCII pur : requests encode les en-têtes en latin-1 (le « œ » casse).
HEADERS = {"User-Agent": "Mozilla/5.0 (AIODOS - recherche locale)"}

MAX_BYTES = 5 * 1024 * 1024        # corps d'une page lu au plus
TOTAL_TIMEOUT = 30.0               # durée totale d'une lecture, redirections comprises
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 15.0                # entre deux lectures du réseau
MAX_REDIRECTS = 5
# Types lus comme du texte ; le reste (image, PDF, binaire) est ignoré sans être lu
_TEXT_TYPES = ("text/", "application/xhtml+xml", "application/xml", "application/json",
               "application/rss+xml", "application/atom+xml")


class FetchStopped(Exception):
    """Lecture arrêtée à la demande de l'appelant (stop)."""


class PageUnreadable(Exception):
    """Page illisible, avec une raison lisible par l'utilisateur."""


def _too_slow() -> "PageUnreadable":
    return PageUnreadable(t("web.page_too_slow", seconds=f"{TOTAL_TIMEOUT:.0f}"))


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


def _type_ok(content_type: str) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    return not kind or kind.startswith(_TEXT_TYPES)


def _web_charsets() -> dict:
    """Les jeux de caractères qu'une page peut annoncer : les étiquettes de la norme
    Encoding du WHATWG (celles que suivent les navigateurs), vers le codec Python qui les
    décode, tous écrits en C et en temps linéaire. Le nom vient du serveur : la
    bibliothèque standard connaît aussi des codecs qui ne sont pas des jeux de caractères
    du Web. Les uns rendent des octets ou décompressent au-delà de MAX_BYTES (zlib, bz2,
    base64…), d'autres décodent en pur Python, en temps plus que quadratique (punycode :
    38 s pour 800 Ko, des heures pour MAX_BYTES, après l'échéance et sans arrêt possible ;
    relecture du 21/10/2026), d'autres encore n'existent sur aucune page (utf-7,
    unicode_escape, idna). Un nom hors de cette liste est ignoré, comme le font les
    navigateurs."""
    table = {
        "utf-8": ("utf-8", "utf8", "unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8",
                  "x-unicode20utf8"),
        # i18n: tel quel (noms de codecs)
        "utf-16-le": ("utf-16", "utf-16le", "ucs-2", "unicode", "csunicode",
                      "iso-10646-ucs-2", "unicodefeff"),
        "utf-16-be": ("utf-16be", "unicodefffe"),
        # Latin-1 et ASCII annoncés sont lus en Windows-1252, comme le font les navigateurs
        "cp1252": ("windows-1252", "x-cp1252", "cp1252", "iso-8859-1", "iso8859-1",
                   "iso88591", "iso_8859-1", "iso_8859-1:1987", "latin1", "l1", "ascii",
                   "us-ascii", "ansi_x3.4-1968", "cp819", "ibm819", "csisolatin1",
                   "iso-ir-100"),
        "cp1254": ("iso-8859-9", "iso8859-9", "iso88599", "iso_8859-9", "latin5", "l5"),
        "cp874": ("windows-874", "dos-874", "iso-8859-11", "iso8859-11", "iso885911",
                  "tis-620"),
        "cp866": ("ibm866", "866", "cp866", "csibm866"),
        "koi8_r": ("koi8-r", "koi8_r", "koi8", "koi", "cskoi8r"),
        "koi8_u": ("koi8-u", "koi8-ru"),
        "mac_roman": ("macintosh", "mac", "x-mac-roman", "csmacintosh"),
        "mac_cyrillic": ("x-mac-cyrillic", "x-mac-ukrainian"),
        "gb18030": ("gb18030", "gbk", "gb2312", "chinese", "csgb2312", "csiso58gb231280",
                    "gb_2312", "gb_2312-80", "iso-ir-58", "x-gbk"),
        "big5hkscs": ("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"),
        "euc_jp": ("euc-jp", "cseucpkdfmtjapanese", "x-euc-jp"),
        "iso2022_jp": ("iso-2022-jp", "csiso2022jp"),
        "cp932": ("shift_jis", "shift-jis", "csshiftjis", "ms932", "ms_kanji", "sjis",
                  "windows-31j", "x-sjis"),
        "cp949": ("euc-kr", "cseuckr", "csksc56011987", "iso-ir-149", "korean",
                  "ks_c_5601-1987", "ks_c_5601-1989", "ksc5601", "ksc_5601", "windows-949"),
    }
    others = {2: ("latin2", "l2"), 3: ("latin3", "l3"), 4: ("latin4", "l4"),
              5: ("cyrillic",), 6: ("arabic",), 7: ("greek", "greek8"),
              8: ("hebrew", "iso-8859-8-i", "visual", "logical"), 10: ("latin6", "l6"),
              13: (), 14: (), 15: ("latin9", "l9"), 16: ()}
    for n, names in others.items():
        table[f"iso8859_{n}"] = (f"iso-8859-{n}", f"iso8859-{n}", f"iso8859{n}",
                                 f"iso_8859-{n}") + names
    for n in range(1250, 1259):                     # windows-1250 à windows-1258
        table[f"cp{n}"] = table.get(f"cp{n}", ()) + (f"windows-{n}", f"cp{n}", f"x-cp{n}")
    return {label: codec for codec, labels in table.items() for label in labels}


WEB_CHARSETS = _web_charsets()
# i18n: tel quel (noms de codecs)
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_DECLARED = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([\w.:-]{1,40})"""
                       rb"""|<\?xml[^>]{0,200}?encoding\s*=\s*["']([\w.:-]{1,40})""", re.I)


def _charset(content_type: str):
    match = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", content_type or "", re.I)
    return match.group(1) if match else None


def _web_codec(label):
    """Le codec d'un jeu de caractères du Web (WEB_CHARSETS), None pour tout autre nom."""
    return WEB_CHARSETS.get(label.strip().lower()) if isinstance(label, str) else None


def _declared_in_page(raw: bytes):
    """Le jeu de caractères que la page déclare elle-même dans son premier Ko (<meta
    charset>, déclaration XML), comme le cherchent les navigateurs. Une page qu'on lit en
    ASCII pour y trouver cette déclaration n'est pas en UTF-16 : elle est alors en UTF-8."""
    match = _DECLARED.search(raw[:1024])
    if not match:
        return None
    codec = _web_codec((match.group(1) or match.group(2)).decode("ascii", "replace"))
    return "utf-8" if codec in ("utf-16-le", "utf-16-be") else codec   # i18n: tel quel (codecs)


def _decoded(raw: bytes, codec: str) -> str:
    """raw dans ce codec, chaque octet fautif remplacé (pas la page entière lue dans un autre
    jeu). Une coupe à MAX_BYTES peut tomber au milieu d'un caractère : il est laissé de
    côté."""
    return codecs.getincrementaldecoder(codec)("replace").decode(raw, final=False)


def _decode(raw: bytes, content_type: str) -> str:
    """Le texte de ce qui a été lu, comme le décident les navigateurs : marque d'ordre des
    octets, sinon jeu de caractères annoncé par le serveur, sinon déclaré dans la page, à
    condition que ce soit un jeu du Web (WEB_CHARSETS). Sans rien de cela : UTF-8 si le
    texte en est presque entièrement (un octet abîmé n'y change rien), Windows-1252 sinon
    (un « é » isolé en Windows-1252 n'est pas de l'UTF-8 valide)."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return _decoded(raw[len(bom):], codec)
    codec = _web_codec(_charset(content_type)) or _declared_in_page(raw)
    if codec:
        return _decoded(raw, codec)
    text = _decoded(raw, "utf-8")
    bad = text.count("\ufffd")
    if not bad:
        return text
    well_formed = len(text) - len(text.encode("ascii", "ignore")) - bad
    if well_formed >= 4 * bad:
        return text
    return _decoded(raw, "cp1252")


def _stream_body(response, guard, stop, deadline, limit=None, slow=_too_slow) -> bytes:
    """Le corps, par blocs, jusqu'à `limit` octets (MAX_BYTES par défaut) ; arrêt au délai
    (slow() levée) ou sur demande (le fil de garde coupe aussi la connexion quand une
    lecture reste bloquée)."""
    limit = MAX_BYTES if limit is None else limit
    chunks, total = [], 0
    for chunk in response.iter_content(64 * 1024):
        if stop is not None and stop.is_set():
            raise FetchStopped()
        if guard.was_cut or time.monotonic() > deadline:
            raise slow()
        if not chunk:
            continue
        room = limit - total
        if len(chunk) >= room:
            chunks.append(chunk[:room])
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _read(session, url, guard, stop, deadline):
    """Suit les redirections à la main (au plus MAX_REDIRECTS) : chaque saut repasse par
    le contrôle du schéma, et sa connexion par la garde des adresses.
    → (url finale, type de contenu, texte)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise addresses.BlockedAddress(t("web.scheme_refused"))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _too_slow()
        response = session.get(current, headers=HEADERS, stream=True, allow_redirects=False,
                               timeout=(min(CONNECT_TIMEOUT, remaining),
                                        min(READ_TIMEOUT, remaining)))
        try:
            if not guard.joined:
                # Aucune connexion n'est passée par la garde : une version de requests ou
                # d'urllib3 la contourne (addresses.supported). Rien de lu n'est rendu
                raise addresses.BlockedAddress(addresses.guard_bypassed())
            if response.is_redirect and response.headers.get("Location"):
                current = urljoin(current, response.headers["Location"])
                continue
            if response.status_code >= 400:
                raise PageUnreadable(t("web.page_error", status=response.status_code))
            content_type = response.headers.get("Content-Type", "")
            if not _type_ok(content_type):
                return current, content_type, ""
            raw = _stream_body(response, guard, stop, deadline)
        finally:
            response.close()
        return current, content_type, _decode(raw, content_type)
    raise PageUnreadable(t("web.too_many_redirects", n=MAX_REDIRECTS))


def _watch(guard, stop, deadline, done):
    """Fil de garde : coupe la connexion au délai, ou dès que l'arrêt est demandé."""
    while True:
        left = deadline - time.monotonic()
        if done.wait(min(0.25, max(0.0, left)) if stop is not None else max(0.0, left)):
            return
        if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
            guard.cut()
            return


@contextmanager
def _guarded(public_only, deadline, stop, name):
    """Une session requests dont chaque connexion passe par la garde des adresses, ni
    proxy (l'adresse jointe serait la sienne) ni .netrc, et un fil de garde qui la coupe au
    délai, ou dès que stop est levé. → (session, garde)"""
    guard = addresses.Guard(public_only=public_only, deadline=deadline)
    done = threading.Event()
    session = requests.Session()
    session.trust_env = False
    adapter = addresses.GuardedAdapter(guard, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    threading.Thread(target=_watch, args=(guard, stop, deadline, done), daemon=True,
                     name=name).start()
    try:
        yield session, guard
    finally:
        done.set()
        session.close()


def _failure(exc, guard, stop, slow, what) -> Exception:
    """L'exception à lever, en clair, pour un échec du réseau pendant une lecture. what :
    « page » ou « search » (web.page_timeout, web.search_unreachable…)."""
    if stop is not None and stop.is_set():
        return FetchStopped()
    if guard.was_cut:
        return slow()
    if isinstance(exc, requests.Timeout):
        return PageUnreadable(t(f"web.{what}_timeout"))
    if isinstance(exc, requests.RequestException):
        return PageUnreadable(t(f"web.{what}_unreachable"))
    return exc


def fetch_url_text(url, max_chars=20000, *, public_only=True, stop=None):
    """(titre, texte) d'une page web, ou du texte brut si ce n'est pas du HTML ; texte
    vide si la page n'est pas du texte (image, PDF…).

    public_only (vrai par défaut) : adresses publiques seulement. Seule une page du réseau
    local demandée sur cette machine passe False (jobs/kinds/index.py). stop : Event qui
    arrête la lecture (FetchStopped). Une page illisible lève PageUnreadable, une adresse
    refusée addresses.BlockedAddress, avec des messages lisibles."""
    deadline = time.monotonic() + TOTAL_TIMEOUT
    with _guarded(public_only, deadline, stop, "aiodos-lecture-page") as (session, guard):
        try:
            final_url, content_type, body = _read(session, url, guard, stop, deadline)
        except (addresses.BlockedAddress, FetchStopped, PageUnreadable):
            raise
        except Exception as exc:
            failure = _failure(exc, guard, stop, _too_slow, "page")
            if failure is exc:
                raise
            raise failure from exc
    if "html" in content_type.lower() or "<html" in body[:1500].lower():
        title, text = html_to_text(body)
        return title or final_url, text[:max_chars]
    return final_url, body[:max_chars]


# ------------------------------------------------------------------ recherche
SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_MAX_BYTES = 2 * 1024 * 1024     # page de résultats lue au plus
SEARCH_TIMEOUT = 20.0                  # durée totale d'une recherche
MAX_TITLE = 300                        # caractères gardés du titre d'un résultat


def _search_too_slow() -> "PageUnreadable":
    return PageUnreadable(t("web.search_too_slow", seconds=f"{SEARCH_TIMEOUT:.0f}"))


class _Results(HTMLParser):
    """Les liens de résultats de la page HTML de DuckDuckGo (<a class="result__a">), dans
    l'ordre : (adresse, titre). html.parser plutôt qu'une expression régulière, qui
    reviendrait en arrière sur une longue suite de « <a » sans « > »."""

    def __init__(self):
        super().__init__()
        self.found, self._href, self._title = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        if "result__a" in (attrs.get("class") or "").split() and attrs.get("href"):
            self._href, self._title = attrs["href"], []

    def handle_data(self, data):
        if self._href is not None:
            self._title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.found.append((self._href, "".join(self._title)))
            self._href = None


def _result_url(href: str) -> str | None:
    """L'adresse d'un résultat, http(s) seulement ; None pour une publicité, un lien de
    DuckDuckGo lui-même, ou tout autre schéma (javascript:, search-ms:, file:…), qui
    deviendrait un lien dans les sources de la réponse."""
    if "y.js" in href or "ad_domain=" in href:          # publicité DuckDuckGo
        return None
    if "uddg=" in href:                                  # lien de redirection
        href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href.strip())
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not host:
        return None
    if host.rstrip(".").lower().endswith("duckduckgo.com"):
        return None
    return href.strip()


def web_search(query, max_results=3, stop=None):
    """Recherche publique via DuckDuckGo (version HTML). → [{title, url}], des adresses
    http(s) seulement.

    Bornée comme une lecture de page (relecture du 21/10/2026) : la réponse est lue en
    flux, SEARCH_MAX_BYTES au plus, en SEARCH_TIMEOUT en tout (le délai de requests ne vaut
    qu'entre deux lectures), par la garde des adresses (Internet public seulement), sans
    proxy ni .netrc ; stop (Stop pendant la préparation d'une réponse) l'interrompt
    (FetchStopped). Une erreur lève PageUnreadable, avec un message lisible."""
    if stop is not None and stop.is_set():
        raise FetchStopped()
    deadline = time.monotonic() + SEARCH_TIMEOUT
    with _guarded(True, deadline, stop, "aiodos-recherche-web") as (session, guard):
        try:
            response = session.post(SEARCH_URL, data={"q": query}, headers=HEADERS,
                                    stream=True, allow_redirects=False,
                                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            try:
                if not guard.joined:
                    raise addresses.BlockedAddress(addresses.guard_bypassed())
                if response.status_code >= 300:
                    raise PageUnreadable(t("web.search_error", status=response.status_code))
                content_type = response.headers.get("Content-Type", "")
                raw = _stream_body(response, guard, stop, deadline, SEARCH_MAX_BYTES,
                                   _search_too_slow)
            finally:
                response.close()
        except (addresses.BlockedAddress, FetchStopped, PageUnreadable):
            raise
        except Exception as exc:
            failure = _failure(exc, guard, stop, _search_too_slow, "search")
            if failure is exc:
                raise
            raise failure from exc
    parser = _Results()
    parser.feed(_decode(raw, content_type))
    results = []
    for href, title in parser.found:
        url = _result_url(href)
        if url is None:
            continue
        results.append({"title": " ".join(title.split())[:MAX_TITLE], "url": url})
        if len(results) >= max_results:
            break
    return results
