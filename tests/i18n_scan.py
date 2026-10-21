"""Outils des tests de traduction (test_i18n_catalogs.py) : les chaînes d'un script de
l'interface ou d'un module Python, et ce qui ressemble à un texte écrit en dur.

Un texte visible ne s'écrit plus dans le code mais dans aiodos/i18n/<langue>/*.json. Une
ligne qui garde exprès une chaîne en clair (motif de détection, identifiant d'un protocole,
journal) porte la marque « i18n: tel quel » en commentaire, sur sa ligne ou celle du dessus.
"""
import ast
import re
from pathlib import Path

MARK = "i18n: tel quel"

# Lettres et signes propres au français
FRENCH_CHARS = re.compile(r"[àâçéèêëîïôûùüÿœæÀÂÇÉÈÊËÎÏÔÛÙÜŸŒÆ«»’]")
# Petits mots du français qu'un texte anglais ne contient pas (ni un nom de classe ou
# d'icône) : ils trahissent une phrase française même sans accent
FUNCTION_WORDS = {
    "le", "la", "les", "des", "du", "une", "est", "pour", "avec", "dans", "sur", "pas",
    "vous", "tu", "ton", "tes", "ta", "cette", "ces", "sont", "qui", "que", "aux", "au",
    "et", "mais", "sans", "chez", "ici", "rien", "aucun", "aucune", "il", "elle", "nous",
    "leur", "leurs", "ses", "mes", "mon", "ma", "notre", "votre", "puis", "encore",
    "jamais", "toujours", "peut", "faut", "aussi", "par", "entre", "selon", "ou", "ni",
    "lui", "eux", "cet", "quand", "dont",
}
# Et des mots courants de l'interface, en français seulement
FRENCH_WORDS = FUNCTION_WORDS | {
    "nouvelle", "nouveau", "fichier", "dossier", "erreur", "envoyer", "annuler",
    "enregistrer", "supprimer", "ajouter", "choisir", "ouvrir", "fermer", "partager",
    "chercher", "installer", "mettre", "jour", "jours", "heure", "heures", "autre",
    "autres", "tous", "toutes", "seulement", "ensuite", "depuis", "pendant", "avant",
    "appairer", "appairage", "attente", "cours", "automatique",
}
WORD = re.compile(r"[A-Za-zÀ-ÿœŒ'](?:[A-Za-zÀ-ÿœŒ']|-(?=[A-Za-zÀ-ÿ]))*")   # sans-serif : un mot
ELISION = re.compile(r"(?:l|d|n|s|j|c|m|qu)'[a-zà-ÿ]")    # l'icône, d'abord, qu'il
# Dans un morceau de HTML : les balises et les attributs qui ne s'affichent pas
MARKUP = re.compile(
    r"""</?[A-Za-z][\w-]*|/?>|\b(?:class|id|style|type|href|src|role|name|value|for|rel|"""
    r"""data-[\w-]+|width|height|min|max|step|rows|autocomplete|accept|target|download|"""
    r"""draggable|tabindex|loading|controls|preload|aria-hidden|viewBox|d|fill|stroke|"""
    r"""stroke-width|stroke-linecap|stroke-linejoin|xmlns|disabled|checked|selected|"""
    r"""hidden|open|multiple|readonly|spellcheck|inputmode|pattern|lang|dir)\s*=\s*"""
    r"""("[^"]*"|'[^']*')""")


def looks_like_text(text: str) -> bool:
    """Un texte destiné à quelqu'un (français, ou une phrase), pas du code : ni sélecteur,
    ni liste de classes, ni identifiant."""
    plain = MARKUP.sub(" ", text)
    if FRENCH_CHARS.search(plain):
        return True
    words = [w.lower() for w in WORD.findall(plain)]
    if len(words) < 2:
        return False
    if any(w in FRENCH_WORDS or ELISION.match(w) for w in words):
        return True
    # Une phrase : une majuscule suivie de minuscules, puis un espace et un autre mot
    return bool(re.search(r"(^|[\s>])[A-Z][a-z]+ [a-z]{2,}", plain))


# ------------------------------------------------------------------------ scripts JS
_KEYWORDS_BEFORE_REGEX = {"return", "typeof", "case", "do", "else", "in", "of", "new",
                          "delete", "void", "throw", "await", "yield"}


def js_literals(source: str) -> list:
    """[(ligne, texte)] : les chaînes '…' et "…", et les morceaux fixes des `…` (hors des
    ${…}), sans les commentaires ni les expressions régulières."""
    out = []
    i, n, line = 0, len(source), 1
    templates = []           # profondeur d'accolades de chaque ${ ouvert
    last = ""                # dernier signe ou mot significatif (regex ou division ?)

    def read_template(start, start_line):
        """Lit un morceau de `…` jusqu'au prochain ${ ou à la fin : (fin, ligne, fermé)."""
        j, ln, chunk = start, start_line, []
        while j < n:
            c = source[j]
            if c == "\\":
                chunk.append(source[j:j + 2])
                ln += source[j:j + 2].count("\n")
                j += 2
                continue
            if c == "`":
                out.append((start_line, "".join(chunk)))
                return j + 1, ln, True
            if c == "$" and source[j + 1:j + 2] == "{":
                out.append((start_line, "".join(chunk)))
                return j + 2, ln, False
            if c == "\n":
                ln += 1
            chunk.append(c)
            j += 1
        return j, ln, True

    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += source[i:end].count("\n")
            i = end
            continue
        if c in "'\"":
            j, chunk = i + 1, []
            while j < n and source[j] != c:
                if source[j] == "\\":
                    chunk.append(source[j:j + 2])
                    j += 2
                    continue
                if source[j] == "\n":
                    break
                chunk.append(source[j])
                j += 1
            out.append((line, "".join(chunk)))
            i = j + 1
            last = "str"
            continue
        if c == "`":
            i, line, closed = read_template(i + 1, line)
            if not closed:
                templates.append(0)
            last = "str"
            continue
        if c == "{" and templates:
            templates[-1] += 1
        if c == "}" and templates:
            if templates[-1] == 0:
                templates.pop()
                i, line, closed = read_template(i + 1, line)
                if not closed:
                    templates.append(0)
                last = "str"
                continue
            templates[-1] -= 1
        if c == "/":
            if last in ("", "op") or last in _KEYWORDS_BEFORE_REGEX:
                j, in_class = i + 1, False
                while j < n and source[j] != "\n":
                    if source[j] == "\\":
                        j += 2
                        continue
                    if source[j] == "[":
                        in_class = True
                    elif source[j] == "]":
                        in_class = False
                    elif source[j] == "/" and not in_class:
                        break
                    j += 1
                j += 1
                while j < n and source[j].isalpha():
                    j += 1
                i = j
                last = "regex"
                continue
            last = "op"
            i += 1
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (source[j].isalnum() or source[j] in "_$"):
                j += 1
            word = source[i:j]
            last = word if word in _KEYWORDS_BEFORE_REGEX else "id"
            i = j
            continue
        last = "id" if c in ")]" else "op"
        i += 1
    return out


def marked(lines: list, line: int) -> bool:
    here = lines[line - 1] if 0 < line <= len(lines) else ""
    above = lines[line - 2] if 1 < line <= len(lines) + 1 else ""
    return MARK in here or MARK in above


def hardcoded_js(path: Path) -> list:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    return [(line, text) for line, text in js_literals(source)
            if looks_like_text(text) and not marked(lines, line)]


# --------------------------------------------------------------------- modules Python
LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
KEY_CALLS = {"t", "lazy", "exists"}


def py_literals(path: Path) -> list:
    """[(début de son instruction, dernière ligne, texte, ligne)] : les chaînes d'un module,
    sans docstrings, journaux (log.info…), ni clés de textes (t("…"), lazy("…"))."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(body[0].value)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or node in docs:
            continue
        holder = parents.get(node)
        while isinstance(holder, (ast.JoinedStr, ast.FormattedValue, ast.BinOp)):
            holder = parents.get(holder)
        if isinstance(holder, ast.Expr):
            continue                                   # chaîne isolée (commentaire)
        if isinstance(holder, ast.Call):
            func = holder.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in LOG_CALLS and isinstance(func, ast.Attribute):
                continue
            if name in KEY_CALLS and holder.args and holder.args[0] is node:
                continue
        statement = node
        while statement is not None and not isinstance(statement, ast.stmt):
            statement = parents.get(statement)
        start = statement.lineno if statement is not None else node.lineno
        found.append((min(start, node.lineno), getattr(node, "end_lineno", node.lineno),
                      node.value, node.lineno))
    return found


def hardcoded_py(path: Path) -> list:
    """[(ligne, texte)] des chaînes qui ressemblent à un texte, sans la marque « tel quel »
    (sur la ligne de l'instruction qui la contient, celle du dessus, ou les siennes)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for start, last, text, line in py_literals(path):
        if not looks_like_text(text):
            continue
        if any(MARK in lines[k - 1] for k in range(max(1, start - 1), min(len(lines), last) + 1)):
            continue
        out.append((line, text))
    return out
