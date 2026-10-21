"""Les scripts de l'interface essayés sous Node.js (tests de sécurité) : i18n.js et les
textes d'une langue, posés avant que les vues ne s'en servent."""
import json
from pathlib import Path

from aiodos import i18n, paths


def install(folder: Path, lang: str = "fr", module: str = "./i18n.js") -> str:
    """Copie i18n.js dans folder et les textes à côté ; → le prélude à mettre en tête du
    script d'essai (module : le chemin d'i18n.js vu de ce script)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "i18n.js").write_bytes((paths.STATIC_DIR / "js" / "i18n.js").read_bytes())
    texts = folder / f"textes-{lang}.json"
    texts.write_text(json.dumps(i18n.texts(lang), ensure_ascii=False), encoding="utf-8")
    return (f"import {{ readFileSync as readTexts }} from 'node:fs';\n"
            f"import {{ setTexts }} from '{module}';\n"
            f"setTexts('{lang}', JSON.parse(readTexts({json.dumps(str(texts))}, 'utf8')));\n")
