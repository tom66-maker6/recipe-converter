"""
ingredient_db.py — deterministic ingredient standardization + learning store.

Guarantees CONSISTENCY: once a name maps to a canonical form, that exact form is
reused forever. The LLM proposes raw names; this layer disposes. Unknown names
pass through (Title-Cased) and are flagged so confidence drops — never invented.
"""
import json, re, unicodedata, difflib

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
# preparation STATES — same raw material, different form -> keep the name as written
# (melted butter, whipped cream, melted chocolate, toasted nuts…), never reduce to base.
_STATE_WORDS = ("melted", "whipped", "toasted", "roasted", "caramelized", "caramelised",
                "softened", "soft", "browned", "fondu", "fondue", "montee", "monte",
                "grille", "torref", "caramelise", "pomade", "pommade")
# CUT / PREPARATION forms — the same base ingredient in another shape. A recipe using
# "Chopped Apricot", "Grated Carrots", "Lemon Puree" or "Walnut Chopped" is not an
# unknown ingredient — keep the name as written and treat it as recognised.
_FORM_WORDS = ("chopped", "grated", "diced", "sliced", "minced", "crushed", "ground",
               "grinded", "shredded", "flaked", "slivered", "halved", "quartered",
               "peeled", "pitted", "seeded", "deseeded", "puree", "pureed", "pulp",
               "mashed", "sifted", "strained", "poached", "candied", "glazed", "smoked",
               "cubed", "julienne", "zested", "concasse", "hache", "rape", "coupe",
               "confit", "compote", "coulis")
# Common abbreviations the software must understand and expand automatically.
_ABBREV = [(r"\bpwdr?\b", "powder"), (r"\bpwd\b", "powder"), (r"\bpdr\b", "powder"),
           (r"\bpdre\b", "powder"), (r"\bb\.?\s*soda\b", "baking soda"),
           (r"\bbicarb(?:onate)?(?:\s+of\s+soda)?\b", "baking soda"), (r"\bbkg\b", "baking"),
           (r"\bchoc\b", "chocolate"), (r"\bveg\b", "vegetable"), (r"\bcornflr\b", "corn flour")]
# oil TYPES to keep as written; a bare "oil" defaults to Grapeseed Oil.
_OIL_KEEP = ("olive", "sunflower", "grapeseed", "grape seed", "vegetable", "canola",
             "rapeseed", "colza", "corn", "peanut", "groundnut", "sesame", "coconut",
             "walnut", "hazelnut", "avocado", "almond", "truffle", "tournesol", "pepin")

def _expand_abbrev(name: str) -> str:
    out = name
    for pat, rep in _ABBREV:
        out = re.sub(pat, rep, out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()

# Base vocabulary for typo tolerance — the type/rule words plus common descriptors,
# so misspellings (Millk, Freash, Roated, Penuts) snap back even when the AI is down.
_SEED_VOCAB = (
    "milk", "lait", "cream", "creme", "flour", "farine", "egg", "eggs", "oeuf", "oeufs",
    "yolk", "yolks", "jaune", "jaunes", "white", "whites", "blanc", "blancs", "whole",
    "chocolate", "chocolat", "couverture", "cocoa", "cacao", "butter", "beurre", "sugar",
    "sucre", "water", "salt", "yeast", "fresh", "dry", "active", "instant", "roasted",
    "toasted", "salted", "unsalted", "melted", "whipped", "softened", "peanut", "peanuts",
    "almond", "almonds", "hazelnut", "hazelnuts", "pistachio", "walnut", "walnuts", "pecan",
    "pecans", "cashew", "cashews", "coconut", "desiccated", "vanilla", "gelatine", "gelatin",
    "honey", "glucose", "cornstarch", "corn", "starch", "powder", "paste", "juice", "zest",
    "extract", "syrup", "inverted", "invert", "brown", "icing", "caster", "cheese",
    "mascarpone", "ricotta", "philadelphia", "cinnamon", "nutmeg", "ginger", "lemon",
    "orange", "coffee", "praline", "raisins", "sultanas", "baking", "soda", "cream",
    "buttermilk", "condensed", "evaporated", "mixture", "batter", "sauce",
)
# words with digits/percent/symbols are left untouched (e.g. '70%', 'T45')
_HAS_SYMBOL = re.compile(r"[0-9%()/.,]")

class IngredientDB:
    def __init__(self, path):
        self.path = path
        raw = json.load(open(path, encoding="utf-8"))
        self.aliases = {_key(k): v for k, v in raw.get("aliases", {}).items()}
        self.known = set(raw.get("known_canonicals", []))
        self._build_vocab()

    def _build_vocab(self):
        """Word list for typo tolerance — built ONLY from correctly-spelled sources
        (seed + states + alias *values* + canonical names). Alias KEYS are excluded
        on purpose: they may hold deliberate misspellings (e.g. 'caster suger'), which
        would otherwise mark a typo as 'correct' and block its correction."""
        vocab = set(_SEED_VOCAB) | set(_STATE_WORDS) | set(_FORM_WORDS)
        for v in self.aliases.values():              # canonical targets only
            vocab.update(_key(v).split())
        for c in self.known:                         # canonical names
            vocab.update(_key(c).split())
        vocab = {w for w in vocab if len(w) >= 3}
        self._vocab = vocab
        self._vocab_list = sorted(vocab)

    def add_correction(self, raw_name: str, canonical: str):
        """Permanently remember raw_name -> canonical and reuse it forever."""
        self.aliases[_key(raw_name)] = canonical
        self.known.add(canonical)
        self._build_vocab()
        self._save()

    def _save(self):
        json.dump({"aliases": self.aliases, "known_canonicals": sorted(self.known)},
                  open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def _spell_correct(self, name: str) -> str:
        """Fix per-word typos against the vocabulary (Millk->milk, Roated->roasted).
        Words with digits/symbols or already-known words are left untouched."""
        out, changed = [], False
        for w in str(name).split():
            wl = _key(w)
            if not wl or _HAS_SYMBOL.search(w) or len(wl) < 4 or wl in self._vocab:
                out.append(w); continue
            m = difflib.get_close_matches(wl, self._vocab_list, n=1, cutoff=0.80)
            if m and m[0] != wl:
                out.append(m[0]); changed = True
            else:
                out.append(w)
        return " ".join(out) if changed else str(name)

    def pre_correct(self, name: str) -> str:
        """Public spelling fix used BEFORE rule detection (e.g. so a typo'd
        'Getaine' becomes 'Gelatine' in time for the gelatine → Gelatine Mass rule)."""
        return self._spell_correct(name)

    def standardize(self, name: str):
        """Return (canonical_name, status). Falls back to typo-correction so common
        misspellings resolve deterministically, even when the AI extractor is off."""
        r = self._standardize_raw(name)
        if r[1] != "unknown":
            return r
        fixed = self._spell_correct(name)                 # typo tolerance, retry once
        if _key(fixed) != _key(name):
            r2 = self._standardize_raw(fixed)
            if r2[1] != "unknown":
                return r2[0], "standardized"
            return _title(fixed), "unknown"               # at least the spelling is fixed
        return r

    def _standardize_raw(self, name: str):
        """Return (canonical_name, status): known / standardized / unknown."""
        name = _expand_abbrev(name)          # Cocoa Pwd -> Cocoa Powder, B. Soda -> Baking Soda
        k = _key(name)

        # 0. Learned corrections + seed glossary ALWAYS win (this is the memory).
        if k in self.aliases:
            c = self.aliases[k]
            return c, ("known" if _key(c) == k else "standardized")

        # 0.5 Preparation STATE or CUT FORM (melted / whipped / chopped / grated / puree…)
        #     — a distinct form of the same material; keep the name as written and treat
        #     it as recognised rather than flagging a perfectly common ingredient.
        if any(re.search(rf"\b{s}\b", k) for s in _STATE_WORDS + _FORM_WORDS):
            return _title(name), "known"

        # 0.6 Oil — a named oil is kept; a bare "oil"/"huile" defaults to Grapeseed Oil.
        if re.search(r"\boil\b|\bhuile\b", k):
            return (_title(name), "known") if any(q in k for q in _OIL_KEEP) \
                   else ("Grapeseed Oil", "standardized")

        # 0.7 Glucose — plain/liquid glucose => Glucose. The dry/atomised form =>
        #     Atomised Glucose. A specified DE grade (e.g. 'Glucose DE 40') is kept.
        if "glucose" in k:
            if any(w in k for w in ("atomis", "atomiz", "dry", "powder", "poudre", "spray")):
                return "Atomised Glucose", "standardized"
            if re.search(r"\bde\s?\d", k):
                return _title(name), "known"
            return "Glucose", "standardized"

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

        # 5. Flour — bare flour/farine => Flour T55; a specified flour (T45, strong,
        #    cake, bread…) is kept. (corn/almond/rice flour are handled by aliases.)
        if re.search(r"\b(flour|farine)\b", k):
            if k in ("flour", "farine", "plain flour", "all purpose flour", "all-purpose flour",
                     "flour t55", "flour t 55", "farine t55"):
                return "Flour T55", "standardized"
            return _title(name), "known"

        # 6. Fallback — Title Case; flag if not a known canonical.
        out = _title(name)
        return out, ("known" if out in self.known else "unknown")
