"""
ingredient_db.py — deterministic ingredient standardization + learning store.

Guarantees CONSISTENCY: once a name maps to a canonical form, that exact form is
reused forever. The LLM proposes raw names; this layer disposes. Unknown names
pass through (Title-Cased) and are flagged so confidence drops — never invented.
"""
import json, re, unicodedata

def _key(s: str) -> str:
    """Accent/ligature/case-insensitive lookup key (handles French)."""
    s = s.replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower().replace("’", "'").replace("`", "'")).strip()

def _title(s: str) -> str:
    return " ".join(w if (any(ch.isdigit() for ch in w) or w.isupper()) else w.capitalize()
                    for w in s.split())

_MILK_KEEP  = ("skim", "ecreme", "demi", "semi", "condens", "concentr", "evapor",
               "ribot", "buttermilk", "almond", "amande", "soja", "soy", "avoine",
               "oat", "coco", "riz", "rice", "poudre", "powder")
_CREAM_KEEP = ("whipping", "fouett", "mont", "aigre", "sour", "double", "epaiss",
               "clotted", "single", "fraiche", "fraich")

class IngredientDB:
    def __init__(self, path):
        self.path = path
        raw = json.load(open(path, encoding="utf-8"))
        self.aliases = {_key(k): v for k, v in raw.get("aliases", {}).items()}
        self.known = set(raw.get("known_canonicals", []))

    def add_correction(self, raw_name: str, canonical: str):
        """Permanently remember raw_name -> canonical and reuse it forever."""
        self.aliases[_key(raw_name)] = canonical
        self.known.add(canonical)
        self._save()

    def _save(self):
        json.dump({"aliases": self.aliases, "known_canonicals": sorted(self.known)},
                  open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def standardize(self, name: str):
        """Return (canonical_name, status): known / standardized / unknown."""
        k = _key(name)

        # 0. Learned corrections + seed glossary ALWAYS win (this is the memory).
        if k in self.aliases:
            c = self.aliases[k]
            return c, ("known" if _key(c) == k else "standardized")

        # 1. Chocolate — translate the LANGUAGE only, never touch the description.
        #    \b after each term stops 'chocolat' from matching inside 'Chocolate'.
        if "chocolat" in k or "chocolate" in k or "couverture" in k:
            out = name
            for fr, en in (("chocolat noir", "Dark Chocolate"), ("chocolat au lait", "Milk Chocolate"),
                           ("chocolat blanc", "White Chocolate"), ("chocolat", "Chocolate")):
                out = re.sub(rf"\b{fr}\b", en, out, flags=re.I)
            return _title(out), "known"

        # 2. Eggs — require a real egg token (so 'white sugar' is NOT an egg white).
        #    No trailing \b, so French plurals (oeufs / jaunes / blancs) still match.
        if re.search(r"\b(egg|oeuf|yolk|jaune)", k):
            prefix = "Fresh " if re.search(r"fresh|frais|fraich", k) else "Pasteurized "
            if "jaune" in k or "yolk" in k:   return prefix + "Egg Yolks", "known"
            if "blanc" in k or "white" in k:  return prefix + "Egg Whites", "known"
            return prefix + "Whole Egg", "known"

        # 3. Milk — bare milk/lait => Whole Milk; a genuinely different type is kept.
        if re.search(r"\b(lait|milk)\b", k):
            return (_title(name), "known") if any(q in k for q in _MILK_KEEP) else ("Whole Milk", "standardized")

        # 4. Cream — bare cream/creme/liquide => Heavy Cream; other type kept.
        if re.search(r"\b(creme|cream)\b", k):
            return (_title(name), "known") if any(q in k for q in _CREAM_KEEP) else ("Heavy Cream", "standardized")

        # 6. Fallback — Title Case; flag if not a known canonical.
        out = _title(name)
        return out, ("known" if out in self.known else "unknown")
