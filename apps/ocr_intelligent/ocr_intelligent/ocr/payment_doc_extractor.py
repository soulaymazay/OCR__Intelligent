# -*- coding: utf-8 -*-
"""
payment_doc_extractor.py — Groupe Bayoudh Metal
VERSION CORRIGÉE v7

CORRECTIONS MAJEURES v7 :
  ✔ Montant : parser robuste gérant "2.520,000 DT" → 2520.0 et "2 520 000" → 2520000
              Priorité : montant en lettres > montant chiffres (plus fiable sur traites)
  ✔ Tireur  : validation renforcée — score de cohérence par token,
              rejet immédiat si >60% tokens ≤2 chars (bruit OCR)
              Fallback : scan des 20 premières lignes cherchant SARL/SA/SUARL
  ✔ N° Traite : patterns étendus capturant "Ordre de paiement LC N° 0008857459455"
                et séquences numériques ≥8 chiffres hors RIB/IBAN
  ✔ Date émission : extraction depuis "Le 02/01/2025" et "Lieu de création … 02/01/2025"
  ✔ Image floue : pipeline d'amélioration multi-passes (super-résolution + débruitage NLM)
  ✔ OCR fallback EasyOCR si PaddleOCR et Tesseract échouent
  ✔ _BRUIT_FORMULAIRE : élargi avec toutes variantes OCR tunisiennes connues
  ✔ Mapping Frappe aligné avec field_matcher.py et ocr_form.js
"""

import re
import os
import math
from datetime import datetime, timedelta

# ──────────────────────────────────────────────────────────────────────
# CONSTANTES MÉTIER
# ──────────────────────────────────────────────────────────────────────

PEREMPTION_CHEQUE_MOIS = 12   # 12 mois en Tunisie (vs 6 mois en France)

_SEUIL_CONTOURS_SIGNATURE   = 40
_SEUIL_QUADRANTS_SIGNATURE  = 2   # assoupli : 2 quadrants suffisent
_SEUIL_IRREGULARITE_CONTOUR = 6.0

_SEUIL_CONFIANCE_PARTIE = 0.30   # abaissé légèrement pour les scans flous
_SEUIL_TOKENS_VALIDES   = 0.55   # ratio tokens "vrais mots" dans un nom de partie

# ──────────────────────────────────────────────────────────────────────
# BANQUES TUNISIENNES
# ──────────────────────────────────────────────────────────────────────

_BANQUES_TN = [
    "STB", "BNA", "BIAT", "UIB", "ATB", "BT", "BH",
    "Zitouna", "QNB", "UBCI", "ABC", "CIB", "BFT",
    "Attijari", "Wafa", "Amen", "Tunisie Leasing",
    "Arab Tunisian Bank", "Banque de Tunisie",
    "Société Tunisienne de Banque",
    "Banque Nationale Agricole",
    "Banque Internationale Arabe",
    "Banque B",    # banque générique sur formulaires
]

_BANQUES_TN_LOWER    = {b.lower() for b in _BANQUES_TN}
_BANQUES_TN_PREFIXES = [b.lower() for b in _BANQUES_TN]
_BANQUES_PATTERN     = "|".join(re.escape(b) for b in _BANQUES_TN)

# ──────────────────────────────────────────────────────────────────────
# BRUIT FORMULAIRE (textes pré-imprimés capturés par OCR)
# ──────────────────────────────────────────────────────────────────────

_BRUIT_FORMULAIRE = re.compile(
    r"\b(?:"
    r"lui[\s\-]*m[eê]me|fournisseur\s+du|fournisseur|souscripteur|vendeur"
    r"|acheteur|le\s+soussign|pr[ée]sent|ci[\s\-]?dessus|ci[\s\-]?apr[eè]s"
    r"|pour\s+(?:acquit|solde|valeur)|contre\s+(?:cette|la|remise)"
    r"|accept[ée]e?\s+(?:par|le)|payable\s+[àa]|[àa]\s+(?:payer|l[''']ordre)"
    r"|bon\s+pour|valeur\s+(?:en|reçue|re[çc]ue)|en\s+(?:votre|notre)"
    r"|i\s+te\s+v|lui\s+meme|lui\-meme|lui\s+même"
    r"|fournisseur\s+d[uo]|du\s+fournisseur|du\s+vendeur|du\s+souscripteur"
    r"|emetteur|[eé]metteur|cr[eé]ancier|d[eé]biteur|porteur|au\s+porteur"
    r"|à\s+l[''']ordre|ordre\s+de|order\s+de|order\s+of"
    r"|order\s+de\s+paiement|ordre\s+de\s+paiement|bill\s+of\s+exchange"
    r"|lettre\s+de\s+change|republique\s+tunisienne|r[ée]publique\s+tunisienne"
    r"|cnp|nom\s+et\s+adresse|adresse\s+du|signature\s+du|cachet\s+du"
    r"|accept[ée]e?|non\s+endossable|bon\s+pour\s+aval|pour\s+aval"
    r"|sous\s+aval|aval(?:is[eé]|ist)|domicili[eé]"
    r"|à\s+l[''']échéance|[àa]\s+vue"
    r"|payer\s+contre|payez\s+contre|somme\s+de|la\s+somme\s+de"
    r"|contre\s+cette|maquette|specimen|modele|formulaire"
    # Variantes OCR bruité tunisien
    r"|sd\s*,|gee\s+it|blipote|exchanges|ae\s+soh|wee\s+ons|gst\s+oe"
    r"|rae\s+ah|dp\s+pe|cn\s+ase|ase\s+de"
    r")\b",
    re.IGNORECASE,
)

_MOTS_CHAMP_ADJACENT = re.compile(
    r"\b(?:date|adresse|tél|tel|fax|rib|iban|r\.i\.b|agence|échéance|echeance"
    r"|émission|emission|montant|tireur|tiré|bénéficiaire|beneficiaire"
    r"|domiciliation|signature|cachet|ville|code\s+postal|banque|bank"
    r"|numéro|numero|référence|reference|n[°o])\b",
    re.IGNORECASE,
)

_PREFIXES_LABELS = re.compile(
    r"^(?:soci[eé]t[eé]|sarl|sa|suarl|sas|eurl|snc|gie|m|mr|mme|dr)\s*[:\-]\s*",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────
# PATTERNS DÉTECTION TYPE DOCUMENT
# ──────────────────────────────────────────────────────────────────────

_PATTERNS_CHEQUE = [
    r"\bch[eèé]que\b",
    r"\bpayez\s+(?:contre\s+)?(?:ce\s+)?ch[eèé]que\b",
    r"\bp[ao]ye[rz]\b",
    r"\bsomme\s+de\b",
    r"\bà\s+l[''']ordre\s+de\b",
    r"\bnon\s+endossable\b",
    r"\btunis\s*,?\s*le\b",
    r"\bcmc\s*7\b",
    r"\b\d{2}\s+\d{3}\s+\d{4}\s+\d{3}\s+\d{2}\b",
]

_PATTERNS_TRAITE = [
    r"\blettre\s*de\s*change\b",
    r"\btraite\b",
    r"\beffet\s*de\s*commerce\b",
    r"\btireur\b",
    r"\btiré\b",
    r"\bdomiciliataire\b",
    r"\bvaleur\s+en\s+compte\b",
    r"\baval\b",
    r"\bbon\s*pour\s*aval\b",
    r"\bveuillez\s+payer\b",
    r"\béch[eé]ance\b",
    r"\bT[-\s]?\d{4}[-\s]\d{2,6}\b",
    r"\bLC\s*N[°o]?\b",
    r"\bordre\s+de\s+paiement\b",
    r"\bbill\s+of\s+exchange\b",
]

# ──────────────────────────────────────────────────────────────────────
# MAPPING OCR → FIELDNAMES FRAPPE  (aligné avec field_matcher.py)
# ──────────────────────────────────────────────────────────────────────

_MAPPING_FRAPPE_CHEQUE = {
    "numero_cheque":    "reference_no",
    "date_cheque":      "reference_date",
    "cheque_date":      "reference_date",
    "amount":           "paid_amount",
    "banque":           "bank",
    "beneficiaire":     "party",
    "titulaire_compte": "account_holder_name",
    "rib":              "bank_account",
}

_MAPPING_FRAPPE_TRAITE = {
    "numero_traite":  "reference_no",
    "date_echeance":  "reference_date",
    "amount":         "paid_amount",
    "tire":           "bank",
    "tireur":         "party",
    "domiciliation":  "custom_domiciliation",
    "date_emission":  "custom_issue_date",
}

# ──────────────────────────────────────────────────────────────────────
# MONTANT EN LETTRES (tunisien / français)
# ──────────────────────────────────────────────────────────────────────

_UNITES = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
    "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
    "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
    "seize": 16, "dix-sept": 17, "dix sept": 17,
    "dix-huit": 18, "dix huit": 18,
    "dix-neuf": 19, "dix neuf": 19,
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
    "soixante": 60, "soixante-dix": 70, "soixante dix": 70,
    "quatre-vingt": 80, "quatre vingt": 80, "quatre-vingts": 80,
    "quatre-vingt-dix": 90, "quatre vingt dix": 90,
}
_MULTIPLICATEURS = {
    "cent": 100, "cents": 100, "mille": 1000,
    "million": 1_000_000, "millions": 1_000_000,
    "milliard": 1_000_000_000, "milliards": 1_000_000_000,
}


def _lettres_vers_chiffre(texte_lettres):
    s = re.sub(r"[-—–]", " ", (texte_lettres or "").lower().strip())
    s = re.sub(r"\s+", " ", s).strip()
    mots = s.split()
    total = courant = 0
    i = 0
    while i < len(mots):
        mot = mots[i]
        bi  = mot + " " + mots[i + 1] if i + 1 < len(mots) else ""
        tri = bi  + " " + mots[i + 2] if i + 2 < len(mots) else ""
        if tri in _UNITES:
            courant += _UNITES[tri]; i += 3
        elif bi in _UNITES:
            courant += _UNITES[bi]; i += 2
        elif mot in _UNITES:
            courant += _UNITES[mot]; i += 1
        elif mot in ("et",):
            i += 1
        elif mot in _MULTIPLICATEURS:
            mult = _MULTIPLICATEURS[mot]
            if mult == 100:
                courant = (courant if courant > 0 else 1) * 100
            elif mult in (1000, 1_000_000, 1_000_000_000):
                courant = (courant if courant > 0 else 1) * mult
                total  += courant; courant = 0
            i += 1
        else:
            i += 1
    total += courant
    return total if total > 0 else None


def _extraire_montant_lettres(texte):
    if not texte:
        return None
    t = re.sub(r"\s+", " ", texte.lower().replace("\n", " "))
    m = re.search(
        r"(?:somme\s+(?:de|ae)\s+|montant\s+(?:de\s+)?|la\s+somme\s+de\s+)(.*?)(?:dinars?)",
        t, re.IGNORECASE
    )
    if not m:
        return None
    dinars = _lettres_vers_chiffre(m.group(1).strip())
    if dinars is None or dinars < 10:
        return None
    millimes = 0
    m2 = re.search(r"dinars?\s+et\s+(.*?)(?:millimes?|cent(?:imes?)?)\b", t, re.IGNORECASE)
    if m2:
        v = _lettres_vers_chiffre(m2.group(1).strip())
        if v is not None:
            millimes = v
    return round(dinars + millimes / 1000.0, 3)


# ──────────────────────────────────────────────────────────────────────
# PARSER MONTANT CHIFFRES
# Gère : "2.520,000 DT" | "2 520 000" | "TND 15,000,000" | "**2520.00**"
# ──────────────────────────────────────────────────────────────────────

def _parser_montant(val) -> float | None:
    """
    Parse un montant textuel vers un float.

    Gestion exhaustive des formats tunisiens :
      "2.520,000 DT"    → 2520.0
      "2 520,000 DT"    → 2520.0
      "2,520,000 DT"    → 2520000.0  (anglais milliers)
      "TND 15,000,000"  → 15000000.0
      "**2520.00**"     → 2520.0
      "520.000"         → 520.0      (3 décimales tunisiennes)
    """
    if not val:
        return None
    s = re.sub(r"(?i)\s*(TND|DT|dinars?|millimes?|euros?|€)\s*", " ", str(val))
    s = s.strip("* ").strip()
    if not s:
        return None

    # Supprimer espaces insécables et normaux (séparateurs milliers)
    s = re.sub(r"[\s\u00A0\u202F]", "", s)

    # Format anglais strict N,NNN,NNN[.DDD]
    m_en = re.match(r"^(\d{1,3}(?:,\d{3})+)(?:\.(\d{1,3}))?$", s)
    if m_en:
        entier = m_en.group(1).replace(",", "")
        dec    = m_en.group(2) or "0"
        try:
            return float(entier + "." + dec) or None
        except ValueError:
            pass

    # Format mixte : virgule + point
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # "2.520,000" → point = séparateur milliers, virgule = décimal
            s = s.replace(".", "").replace(",", ".")
        else:
            # "2,520.000" → virgule = séparateur milliers
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2:
            after = parts[1]
            if len(after) == 3 and after.isdigit():
                # "2,520" ou "2,000" ambigu → traiter comme milliers si partie entière ≥ 4 chiffres
                # sinon décimal
                if len(parts[0]) >= 4:
                    s = parts[0] + after           # 2000 → 2000
                else:
                    s = parts[0] + "." + after     # 520,000 → 520.000
            else:
                s = s.replace(",", ".")            # 202,11 → 202.11
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        # "2.520" : point unique avec 3 décimales → milliers ou décimal ?
        # Si partie entière ≥ 2 chiffres et exactement 3 décimales → milliers (tunisien)
        if len(parts) == 2 and len(parts[1]) == 3 and parts[0].isdigit() and len(parts[0]) >= 1:
            # "2.520" → 2520  |  "520.000" → 520.000 (déjà décimal correct)
            # Heuristique : si entier < 1000 et 3 décimales → décimal tunisien
            try:
                entier_val = int(parts[0])
                if entier_val < 1000:
                    pass  # on garde "520.000" tel quel
                else:
                    s = parts[0] + parts[1]  # "2.520" → "2520"
            except ValueError:
                pass

    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────
# PRÉTRAITEMENT TEXTE OCR
# ──────────────────────────────────────────────────────────────────────

def _pretraiter_texte_ocr(texte: str) -> str:
    t = texte
    t = re.sub(r"[‐‑‒–—−]", "-", t)
    t = t.replace("／", "/").replace("⁄", "/")
    # Correction O→0 dans les dates uniquement
    t = re.sub(r"(?<=[0-9/\-\.])O(?=[0-9/\-\.])", "0", t)
    t = re.sub(r"(?<=[0-9/\-\.])[lI](?=[0-9/\-\.])", "1", t)
    return t


# ──────────────────────────────────────────────────────────────────────
# AMÉLIORATION IMAGE FLOUE — MULTI-PASSES (NOUVEAU v7)
# ──────────────────────────────────────────────────────────────────────

def _ameliorer_image_floue(chemin_img: str):
    """
    Pipeline d'amélioration pour images floues :
    1. Super-résolution (upscale ×3 Lanczos)
    2. Débruitage NLM
    3. CLAHE contraste
    4. Unsharp mask netteté
    5. Binarisation adaptative

    Retourne (texte_ameliore, score) ou ("", 0) si échec.
    """
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image as PILImage

        ext = os.path.splitext(chemin_img)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_img, dpi=400)
            if not pages:
                return "", 0
            pil_img = pages[0].convert("RGB")
        else:
            pil_img = PILImage.open(chemin_img).convert("RGB")

        w, h = pil_img.size

        # Passe 1 : upscale ×3 si petite image
        scale_factor = 1
        if w < 2000:
            scale_factor = max(3, int(2400 / w))
            pil_img = pil_img.resize(
                (w * scale_factor, h * scale_factor),
                PILImage.LANCZOS
            )

        img_cv = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        # Passe 2 : débruitage NLM
        img_cv = cv2.fastNlMeansDenoisingColored(img_cv, None, 10, 10, 7, 21)

        # Passe 3 : CLAHE
        lab   = cv2.cvtColor(img_cv, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        l     = clahe.apply(l)
        img_cv = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

        # Passe 4 : unsharp mask
        gaussian = cv2.GaussianBlur(img_cv, (9, 9), 10.0)
        img_cv   = cv2.addWeighted(img_cv, 1.8, gaussian, -0.8, 0)

        # Passe 5 : binarisation adaptative sur niveaux de gris
        gray     = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
        thr_adap = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
        )
        _, thr_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        textes = []
        for arr in (gray, thr_adap, thr_otsu):
            for psm in (6, 11, 4, 3):
                try:
                    data = pytesseract.image_to_data(
                        PILImage.fromarray(arr), lang="fra+eng+ara",
                        config=f"--oem 3 --psm {psm}",
                        output_type=pytesseract.Output.DICT
                    )
                    confs   = [int(c) for c in data["conf"] if str(c) != "-1" and int(c) > 0]
                    score   = round(sum(confs) / len(confs), 1) if confs else 0
                    txt     = " ".join(w for w in data["text"] if isinstance(w, str) and w.strip())
                    mots    = len([m for m in txt.split() if len(m) > 1])
                    textes.append((score, mots, txt))
                except Exception:
                    continue

        if not textes:
            return "", 0

        # Sélectionner le meilleur résultat (score * 0.6 + mots_norm * 0.4)
        def crit(t):
            sc, mots, _ = t
            return (sc / 100) * 0.6 + (min(mots, 50) / 50) * 0.4

        best = max(textes, key=crit)
        return best[2], best[0]

    except Exception as e:
        try:
            import frappe
            frappe.logger("ocr").warning(f"[OCR] amélioration image floue échouée : {e}")
        except Exception:
            print(f"[OCR] amélioration image floue échouée : {e}")
        return "", 0


def _tenter_amelioration_texte(chemin_img: str) -> str:
    """Wrapper rétrocompatible."""
    txt, _ = _ameliorer_image_floue(chemin_img)
    return txt


# ──────────────────────────────────────────────────────────────────────
# ÉTAPE 1 — QUALITÉ
# ──────────────────────────────────────────────────────────────────────

def _evaluer_qualite(texte, score_ocr) -> bool:
    mots = [m for m in texte.split() if len(m) > 1]
    if score_ocr < 40 or len(mots) < 5:
        return True
    nb_parasites = len(re.findall(r"[|\\/@#~^<>{}\[\]]{2,}", texte))
    return nb_parasites > 5


# ──────────────────────────────────────────────────────────────────────
# ÉTAPE 2 — IDENTIFICATION DU TYPE
# ──────────────────────────────────────────────────────────────────────

def _identifier_type_document(texte) -> tuple[str, float]:
    t  = texte.lower()
    sc = sum(1 for p in _PATTERNS_CHEQUE if re.search(p, t, re.IGNORECASE))
    st = sum(1 for p in _PATTERNS_TRAITE if re.search(p, t, re.IGNORECASE))
    if sc == 0 and st == 0:
        return "inconnu", 0.0
    total = sc + st
    if sc >= st:
        return "cheque", round(sc / total, 3)
    return "traite", round(st / total, 3)


def _normaliser_payment_method(pm: str) -> str | None:
    if not pm:
        return None
    p = pm.lower().strip()
    if any(k in p for k in ("chèque", "cheque", "check", "chéque")):
        return "cheque"
    if any(k in p for k in ("traite", "lettre de change", "effet", "draft")):
        return "traite"
    return None


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION DATES
# ──────────────────────────────────────────────────────────────────────

_MOIS_FR = {
    "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
    "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
    "août": "08", "aout": "08", "septembre": "09",
    "octobre": "10", "novembre": "11", "décembre": "12", "decembre": "12",
}


def _extraire_dates_brutes(texte: str) -> list:
    pat = r"\d{1,2}\s*[\/\-\.]\s*\d{1,2}\s*[\/\-\.]\s*\d{2,4}"
    dates = []
    annee_max = datetime.now().year + 5

    for m in re.finditer(pat, texte):
        raw     = re.sub(r"\s*([\/\-\.])\s*", r"\1", m.group(0).strip())
        unified = re.sub(r"[\/\-\.]", "/", raw)
        parts   = unified.split("/")
        if len(parts) != 3:
            continue
        try:
            d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        y = 2000 + y if y < 100 else y
        if not (2000 <= y <= annee_max):
            continue
        try:
            dates.append(datetime.strptime(f"{d:02d}/{mo:02d}/{y:04d}", "%d/%m/%Y"))
        except ValueError:
            continue
    return dates


def _normaliser_date(val: str) -> str | None:
    if not val:
        return None
    val = val.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        return val

    m_fr = re.match(
        r"(\d{1,2})\s+(" + "|".join(_MOIS_FR.keys()) + r")\s+(\d{4})",
        val, re.IGNORECASE
    )
    if m_fr:
        dd = m_fr.group(1).zfill(2)
        mm = _MOIS_FR[m_fr.group(2).lower()]
        return f"{m_fr.group(3)}-{mm}-{dd}"

    for fmt in [
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
        "%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d",
    ]:
        try:
            d = datetime.strptime(val, fmt)
            y = d.year + 2000 if d.year < 100 else d.year
            return f"{y}-{d.month:02d}-{d.day:02d}"
        except ValueError:
            continue
    return None


def _anciennete_mois(date_doc) -> float:
    return (datetime.now() - date_doc).days / 30.44


# ──────────────────────────────────────────────────────────────────────
# VALIDATION NOM DE PARTIE — RENFORCÉE v7
# ──────────────────────────────────────────────────────────────────────

def _valider_nom_partie(valeur: str, champ: str) -> str | None:
    """
    Valide un nom de partie (tireur, tiré, bénéficiaire).

    CORRECTIONS v7 :
    - Rejet si score OCR tokens ≤2 chars > 60% (bruit type "3 eh Blipote exchanges...")
    - Rejet immédiat si _BRUIT_FORMULAIRE détecté
    - Pour champ "tire" : chercher banque connue en premier
    """
    if not valeur:
        return None
    v = valeur.strip()

    if _BRUIT_FORMULAIRE.search(v):
        return None
    if len(v) > 80:
        return None

    lettres = re.sub(r"[^A-Za-zÀ-ÿ]", "", v)
    if len(lettres) < 3:
        return None

    # Rejet si trop de chiffres (bruit RIB/numéro)
    if len(re.findall(r"\d", v)) >= 5:
        return None

    # Rejet si trop de caractères parasites
    nb_bruit = len(re.findall(r"[|\\@#~^<>{}\[\]]{1}", v))
    if nb_bruit > 2:
        return None

    # Ratio lettres/total.
    nb_total_no_space = len(re.sub(r"\s", "", v))
    if nb_total_no_space > 0 and len(lettres) / nb_total_no_space < 0.50:
        return None

    # Ratio tokens courts (<=2 chars) pour filtrer le bruit OCR.
    mots = v.split()
    if len(mots) >= 3:
        mots_courts = sum(1 for m in mots if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", m)) <= 2)
        if mots_courts / len(mots) >= 0.60:
            return None

    # Rejet si mot unique qui est un mot-clé de formulaire
    if len(mots) <= 1 and v.lower() in {"nom", "raison", "sociale", "societe", "société",
                                          "tireur", "tiré", "beneficiaire", "bénéficiaire"}:
        return None

    # Pour le champ "tire" : valider que c'est une banque connue
    if champ == "tire":
        v_low = v.lower()
        for banque in _BANQUES_TN_PREFIXES:
            if banque in v_low:
                # Retourner le nom canonique de la banque
                for b in _BANQUES_TN:
                    if b.lower() in v_low:
                        return b
                return v[:50].strip()
        # Pas de banque reconnue mais nom plausible → accepter avec longueur min
        if len(lettres) < 4:
            return None

    return v[:70].strip()


# ──────────────────────────────────────────────────────────────────────
# PATTERNS EXTRACTION CHÈQUE
# ──────────────────────────────────────────────────────────────────────

_PATTERNS_CHAMP_CHEQUE = {
    "numero_cheque": [
        r"(?:n[°o]?\s*(?:du\s*)?ch[eèé]que|ch[eèé]que\s*n[°o]?)\s*[:\-]?\s*([0-9]{4,12})",
        r"(?:numéro|num\.?)\s*[:\-]?\s*([0-9]{4,12})",
        r"\b([0-9]{7,10})\b",
    ],
    "date_cheque": [
        r"(?:tunis\s*,?\s*le|date\s*[:\-]?|le\s+)\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",
        r"(\d{1,2}\s+(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+\d{4})",
    ],
    "montant_chiffres": [
        r"\*+\s*([\d\s\u00a0]+[,\.]\d{2,3})\s*(?:TND|DT|dinars?)?",
        r"(?:montant|somme\s*(?:de)?)\s*[:\-]?\s*([\d\s\u00a0]+[,\.]\d{2,3})\s*(?:TND|DT|dinars?)?",
        r"([\d\s\u00a0]{3,}[,\.]\d{2,3})\s*(?:TND|DT)",
        r"([\d\s\u00a0]{3,})\s*(?:TND|DT|dinars?)\b",
    ],
    "montant_lettres": [
        r"(?:la\s+)?somme\s+(?:de|ae)\s+(.*?)(?:dinars?)\s*(?:et\s+(.*?)(?:millimes?|cent(?:imes?)?))?",
        r"(?:montant\s+(?:en\s+lettres?\s*)?[:\-]?\s*)(.*?)(?:dinars?)",
    ],
    "banque": [
        rf"\b({_BANQUES_PATTERN})\b",
        r"(?:banque|bank|tiré\s+sur|établissement)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{2,50})",
    ],
    "rib": [
        r"\b(\d{2}\s*\d{3}\s*\d{4}\s*\d{9}\s*\d{2})\b",
        r"(?:rib|iban|compte)\s*[:\-]?\s*(\d[\d\s]{14,29})",
    ],
    "beneficiaire": [
        r"(?:[àa]\s+l[''']ordre\s+de|bénéficiaire|ordre\s+de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
        r"(?:payer\s+[àa]|veuillez\s+payer\s+[àa])\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
    ],
    "titulaire_compte": [
        r"(?:titulaire|nom\s+(?:du\s+)?titulaire)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
    ],
    "memo": [
        r"(?:objet|memo|motif|pour|réf\.?|ref\.?)\s*[:\-]?\s*([^\n]{3,80})",
    ],
}

# ──────────────────────────────────────────────────────────────────────
# PATTERNS EXTRACTION TRAITE — CORRIGÉS v7
# Captures : "Ordre de paiement LC N° 0008857459455"
#            "CNP 0008857459455"
#            Séquences ≥ 9 chiffres (hors RIB 20 chiffres)
# ──────────────────────────────────────────────────────────────────────

_PATTERNS_CHAMP_TRAITE = {
    "numero_traite": [
        # "Ordre de paiement LC N° 0008857459455"  ← CAS IMAGE
        r"(?:ordre\s+de\s+paiement|order\s+of\s+payment)\s*(?:lc|cn|cnp)?\s*n[°o]?\s*[:\-=]?\s*([A-Z0-9]{6,20})",
        # "LC N° XXXX" ou "LC-XXXX-YYYY"
        r"\bLC\s*N[°o]?\s*[:\-]?\s*([A-Z0-9][\w\-\/]{1,20})",
        r"\b(LC[-\s]?\d{4,})\b",
        r"\b(T[-\s]?\d{4}[-\s]?\d{2,6})\b",
        r"\bCNP\s*[:\-]?\s*([0-9]{8,14})\b",
        # Label explicite "N° Traite" / "Numéro traite"
        r"(?:n[°o]?\s*(?:de\s*(?:la\s*)?)?traite|traite\s*n[°o]?|numéro\s+traite)\s*[:\-]?\s*([A-Z0-9][\w\-\/]{1,25})",
        # Référence générique — exige ≥1 chiffre
        r"(?:référence|réf\.?|ref\.?|num\.?|n[°o]?)\s*[:\-]\s*([A-Z0-9]{1,4}[\-\/]?\d{4,}[\-\/]?\d{0,6})",
        # Fallback : séquence numérique longue (9-14 chiffres) hors RIB (20 chiffres)
        r"\b(\d{9,14})\b(?!\s*\d)",
    ],
    "date_emission": [
        # "Le 02/01/2025" (champ date sur traite tunisienne)
        r"(?:le\s+)(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})\b",
        # "Lieu de création … 02/01/2025"
        r"(?:lieu\s+de\s+cr[eé]ation|date\s+de\s+cr[eé]ation|[eé]mis(?:e)?\s+le|fait\s+[àa][^,\n]*,?\s*le)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:date\s*d[''']?[eé]mission|date)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",   # fallback: première date
    ],
    "date_echeance": [
        r"(?:date\s*d[''']?[eé]ch[eé]ance|[eé]ch[eé]ance|due\s*date|payable\s*le|[àa]\s*payer\s*le)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:échéance|echeance|ech[eé]ance)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:[àa]\s+en)\s+(\d{1,3})\s*jours?\s*(?:de\s*vue)?",
    ],
    "montant": [
        r"\*+\s*([\d\s\u00a0]+[,\.]\d{2,3})\s*(?:TND|DT|dinars?)?",
        # "2.520,000 DT" ou "2 520,000 DT"
        r"(?:montant|somme\s*(?:de)?|la\s+somme\s+de|valeur|amount)\s*[:\-]?\s*([\d\s\u00a0\.,]+[,\.]\d{2,3})\s*(?:TND|DT|dinars?)?",
        r"\b([\d\s\u00a0\.,]{4,})\s*(?:TND|DT)\b",
    ],
    "montant_lettres": [
        r"(?:la\s+)?somme\s+(?:de|ae)\s+(.*?)(?:dinars?)\s*(?:et\s+(.*?)(?:millimes?|cent(?:imes?)?))?",
        r"(?:deux\s+mille|trois\s+mille|quatre\s+mille|cinq\s+mille)\s+(?:cinq\s+)?cent[^\n]{0,30}dinars?",
    ],
    "tireur": [
        r"(?:tireur|drawer|[eé]mis\s*par|cr[eé]ancier|souscripteur|nom\s+(?:et\s+adresse\s+)?du\s+tireur)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n,\(\)\[\]<>]{2,60})",
        # Ligne avec forme juridique
        r"\b([A-ZÀ-Ü][A-Za-zÀ-ÿ\s&\-\.]{3,40}(?:SARL|SA\b|SUARL|SAS\b|EURL|SNC|GIE))\b",
        # Initiales majuscules sur ≥ 2 mots
        r"\b([A-ZÀ-Ü]{2,}(?:\s+[A-ZÀ-Ü]{2,}){1,4})\b",
    ],
    "tire": [
        rf"\b({_BANQUES_PATTERN})\b",
        r"(?:tiré|drawee|banque\s+(?:payeuse|tirée)|[eé]tablissement\s+(?:payeur|bancaire)|nom\s+(?:et\s+adresse\s+)?du\s+tiré)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n,\(\)\[\]<>0-9]{2,60})",
        # "RIB ou RIP du Tiré" → extraire ce qui suit
        r"rib\s+ou\s+rip\s+du\s+tiré?\b[^\n]*\n([A-Za-zÀ-ü][^\n]{2,50})",
    ],
    "beneficiaire": [
        r"(?:b[eé]n[eé]ficiaire|beneficiary|[àa]\s+l[''']ordre\s+de|ordre\s+de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n,\(\)\[\]<>]{3,60})",
        # "lui même ou fournisseur du tireur" → on ignore, valeur = tireur
    ],
    "domiciliation": [
        r"(?:domiciliataire|domiciliation|banque\s+dom\.?|domicile\s+de\s+paiement|agence\s+bancaire)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n,\(\)\[\]<>]{3,60})",
        r"(?:agence)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n,\(\)\[\]<>]{3,40})",
    ],
}

_CHAMPS_PARTIES = {"tireur", "tire", "beneficiaire", "domiciliation", "banque", "titulaire_compte"}


# ──────────────────────────────────────────────────────────────────────
# UTILITAIRES EXTRACTION
# ──────────────────────────────────────────────────────────────────────

def _tronquer_au_premier_champ_adjacent(valeur: str) -> str:
    starts  = [m.start() for m in _MOTS_CHAMP_ADJACENT.finditer(valeur) if m.start() > 0]
    starts += [m.start() for m in _BRUIT_FORMULAIRE.finditer(valeur) if m.start() > 0]
    if starts:
        return valeur[:min(starts)].strip()
    return valeur


def _nettoyer_valeur(val: str) -> str:
    val = val.strip()
    val = re.sub(r"^[\s:;\-|]+", "", val)
    val = re.sub(r"[\s:;\-|]+$", "", val)
    val = re.sub(r"\s{2,}", " ", val)
    val = val.strip("*").strip()
    val = re.sub(r"\s*\([^)]{0,60}\)\s*$", "", val).strip()
    val = _PREFIXES_LABELS.sub("", val).strip()
    return val


def _extraire_champ_avec_confiance(
    texte: str, patterns: list, nom_champ: str
) -> tuple[str, float, bool]:
    for i, pattern in enumerate(patterns):
        try:
            m = re.search(pattern, texte, re.IGNORECASE | re.MULTILINE)
            if m:
                valeur = (
                    m.group(1).strip() if m.lastindex and m.lastindex >= 1
                    else m.group(0).strip()
                )
                valeur = _nettoyer_valeur(valeur)

                if nom_champ in _CHAMPS_PARTIES:
                    valeur = _tronquer_au_premier_champ_adjacent(valeur)
                    valeur = re.sub(r"[\s:;\-|\u2013\u2014]+$", "", valeur).strip()
                    valeur = re.sub(
                        r"\b(?:et|ou|de|du|des|la|le|l['''])\s*$", "", valeur, flags=re.IGNORECASE
                    ).strip()

                if not valeur:
                    continue

                incertain  = bool(re.search(r"[|\\~@#^<>{}\[\]]{1}", valeur))
                confiance  = max(0.40, 1.0 - i * 0.15)
                if incertain:
                    confiance *= 0.6
                return valeur, confiance, incertain
        except (re.error, IndexError):
            continue
    return "", 0.0, False


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION N° TRAITE HEURISTIQUE
# ──────────────────────────────────────────────────────────────────────

def _extraire_numero_traite_heuristique(texte: str) -> str | None:
    if not texte:
        return None
    lignes = [l.strip() for l in texte.splitlines() if l.strip()]

    # Chercher "Ordre de paiement" sur une ligne, le numéro sur la même ou la suivante
    for i, ln in enumerate(lignes[:80]):
        if re.search(r"ordre\s+de\s+paiement", ln, re.IGNORECASE):
            # Numéro sur la même ligne
            m = re.search(r"\b(\d{8,14})\b", ln)
            if m:
                return m.group(1)
            # Numéro sur la ligne suivante
            if i + 1 < len(lignes):
                m2 = re.search(r"\b(\d{8,14})\b", lignes[i + 1])
                if m2:
                    return m2.group(1)

    # Chercher "LC N°" ou "CNP"
    m_lc = re.search(r"\bLC\s*N[°o]?\s*[:\-]?\s*([A-Z0-9]{4,20})", texte, re.IGNORECASE)
    if m_lc:
        return m_lc.group(1)

    m_cnp = re.search(r"\bCNP\s*[:\-]?\s*([0-9]{8,14})", texte, re.IGNORECASE)
    if m_cnp:
        return m_cnp.group(1)

    # Fallback : séquence 9-14 chiffres hors lignes de RIB/date
    pat_date = re.compile(r"\b\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}\b")
    for ln in lignes[:80]:
        if "rib" in ln.lower() or "iban" in ln.lower():
            continue
        if pat_date.search(ln):
            continue
        m = re.search(r"\b(\d{9,14})\b", ln)
        if m and not re.match(r"^(19|20)\d{2}", m.group(1)):
            return m.group(1)

    return None


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION NOM SOCIÉTÉ HEURISTIQUE
# ──────────────────────────────────────────────────────────────────────

def _extraire_nom_societe_heuristique(texte: str) -> str | None:
    _PAT_FORME_JUR = re.compile(r'\b(?:SARL|SA\b|SUARL|SAS\b|EURL|SNC|GIE)\b')
    _BRUIT_LIGNES  = re.compile(
        r'\b(?:LETTRE|CHANGE|TRAITE|EFFET|TIREUR|TIRÉ|DATE|ÉCHÉANCE|MONTANT'
        r'|BÉNÉFICIAIRE|DOMICILIATION|VALEUR|ACCEPTÉ|SIGNATURE|CACHET|TUNIS'
        r'|REPUBLIQUE|BILL|EXCHANGE)\b',
        re.IGNORECASE
    )
    for ligne in texte.splitlines()[:30]:
        ls = ligne.strip()
        if not ls or len(ls) < 4:
            continue
        if _BRUIT_LIGNES.search(ls) or _BRUIT_FORMULAIRE.search(ls):
            continue
        m_forme = _PAT_FORME_JUR.search(ls)
        if m_forme:
            nom = ls[:m_forme.end()].strip()
            lettres = re.sub(r"[^A-Za-zÀ-ÿ]", "", nom)
            if len(lettres) >= 3:
                return nom[:70]
    return None


# ──────────────────────────────────────────────────────────────────────
# DÉTECTION SIGNATURE
# ──────────────────────────────────────────────────────────────────────

def _detecter_signature(chemin_img: str, texte: str) -> bool:
    mots_sig = [r"\bsign[eéè]\b", r"\bparaph[eé]\b", r"\bcachet\b", r"\bstamp\b"]
    if any(re.search(p, texte, re.IGNORECASE) for p in mots_sig):
        return True

    try:
        import cv2
        import numpy as np

        ext = os.path.splitext(chemin_img)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_img, dpi=200)
            if not pages:
                return False
            img = cv2.cvtColor(np.array(pages[0].convert("RGB")), cv2.COLOR_RGB2BGR)
        else:
            img = cv2.imread(chemin_img)
            if img is None:
                from PIL import Image as PILImage
                pil = PILImage.open(chemin_img).convert("RGB")
                img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

        h, w  = img.shape[:2]
        zone  = img[int(h * 0.65):h, int(w * 0.50):w]
        if zone is None or zone.size == 0:
            return False

        gray   = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        zh, zw = gray.shape[:2]

        _, thresh_d = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        densite     = np.sum(thresh_d > 0) / (zh * zw) if (zh * zw) > 0 else 0
        if densite < 0.015 or densite > 0.65:
            return False

        edges    = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        contours_q = []
        for c in contours:
            per = cv2.arcLength(c, False)
            if per < 15:
                continue
            aire = cv2.contourArea(c)
            if aire < 3:
                continue
            irr = per / math.sqrt(max(aire, 1))
            if irr >= _SEUIL_IRREGULARITE_CONTOUR:
                contours_q.append(c)

        if len(contours_q) < _SEUIL_CONTOURS_SIGNATURE:
            return False

        mid_x, mid_y    = zw // 2, zh // 2
        quadrants_actifs = set()
        for c in contours_q:
            M = cv2.moments(c)
            if M["m00"] == 0:
                x, y, cw, ch = cv2.boundingRect(c)
                cx, cy = x + cw // 2, y + ch // 2
            else:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
            quadrants_actifs.add((0 if cx < mid_x else 1) + (0 if cy < mid_y else 2))

        return len(quadrants_actifs) >= _SEUIL_QUADRANTS_SIGNATURE

    except Exception:
        return False


def _detecter_acceptation(texte: str) -> bool:
    patterns = [r"\baccepté\b", r"\baccepted\b", r"\bbon\s+pour\s+accord\b"]
    return any(re.search(p, texte, re.IGNORECASE) for p in patterns)


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION CHÈQUE
# ──────────────────────────────────────────────────────────────────────

def _extraire_champs_cheque(texte: str) -> tuple[dict, list, float]:
    champs          = {}
    incertains      = []
    scores_confiance = []

    for champ in [
        "numero_cheque", "date_cheque", "montant_chiffres",
        "montant_lettres", "banque", "rib", "beneficiaire",
        "titulaire_compte", "memo",
    ]:
        valeur, conf, incertain = _extraire_champ_avec_confiance(
            texte, _PATTERNS_CHAMP_CHEQUE.get(champ, []), champ
        )
        if champ in _CHAMPS_PARTIES:
            if conf < _SEUIL_CONFIANCE_PARTIE:
                valeur, conf = "", 0.0
            elif valeur:
                valeur_v = _valider_nom_partie(valeur, champ)
                valeur, conf = (valeur_v, conf) if valeur_v else ("", 0.0)

        if valeur:
            champs[champ] = valeur
            scores_confiance.append(conf)
            if incertain:
                incertains.append(champ)
        else:
            champs[champ] = ""

    # Résoudre le montant (lettres prioritaires car plus fiables)
    amount = _extraire_montant_lettres(texte)
    if amount is None and champs.get("montant_chiffres"):
        amount = _parser_montant(champs["montant_chiffres"])

    champs["amount"] = amount if (amount and amount > 0) else 0.0
    if champs["amount"] == 0.0:
        incertains.append("amount")

    if champs.get("date_cheque"):
        date_norm = _normaliser_date(champs["date_cheque"])
        champs["cheque_date"] = date_norm or champs["date_cheque"]
        if not date_norm:
            incertains.append("cheque_date")

    champs["champs_obligatoires_presents"] = bool(
        champs.get("numero_cheque") and
        champs.get("amount", 0) > 0 and
        champs.get("cheque_date")
    )
    confiance = sum(scores_confiance) / len(scores_confiance) if scores_confiance else 0.0
    return champs, list(set(incertains)), confiance


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION TRAITE — CORRIGÉE v7
# ──────────────────────────────────────────────────────────────────────

def _extraire_champs_traite(texte: str) -> tuple[dict, list, float]:
    champs          = {}
    incertains      = []
    scores_confiance = []

    for champ in [
        "numero_traite", "date_emission", "date_echeance",
        "montant", "montant_lettres", "tireur", "tire",
        "beneficiaire", "domiciliation",
    ]:
        valeur, conf, incertain = _extraire_champ_avec_confiance(
            texte, _PATTERNS_CHAMP_TRAITE.get(champ, []), champ
        )

        if champ in _CHAMPS_PARTIES:
            if conf < _SEUIL_CONFIANCE_PARTIE:
                valeur, conf = "", 0.0
            elif valeur:
                valeur_v = _valider_nom_partie(valeur, champ)
                valeur, conf = (valeur_v, conf) if valeur_v else ("", 0.0)

        # N° traite: au moins un chiffre et longueur minimale de 4.
        if champ == "numero_traite" and valeur:
            if not re.search(r"\d", valeur) or len(valeur) < 4:
                valeur, conf = "", 0.0

        if valeur:
            champs[champ] = valeur
            scores_confiance.append(conf)
            if incertain:
                incertains.append(champ)
        else:
            champs[champ] = ""

    # Fallbacks robustes
    if not champs.get("numero_traite"):
        ref = _extraire_numero_traite_heuristique(texte)
        if ref:
            champs["numero_traite"] = ref

    if not champs.get("tireur"):
        t = _extraire_nom_societe_heuristique(texte)
        if t:
            v = _valider_nom_partie(t, "tireur")
            if v:
                champs["tireur"] = v

    # Vérifier "bénéficiaire = lui même ou fournisseur du tireur" → utiliser tireur
    benef = champs.get("beneficiaire", "")
    if benef and re.search(r"lui.?m[eê]me|fournisseur\s+du\s+tireur", benef, re.IGNORECASE):
        champs["beneficiaire"] = champs.get("tireur", "")

    # ── MONTANT — priorité lettres > chiffres ──
    amount = _extraire_montant_lettres(texte)
    if amount is None and champs.get("montant"):
        amount = _parser_montant(champs["montant"])

    champs["amount"] = amount if (amount and amount > 0) else 0.0
    if champs["amount"] == 0.0:
        incertains.append("amount")

    # ── DATES ──
    toutes_dates = sorted(_extraire_dates_brutes(texte))

    def _norm_date(champ_date: str) -> str | None:
        v = champs.get(champ_date)
        if not v:
            return None
        m_jours = re.match(r"^(\d+)\s*(?:jours?)?$", v.strip())
        if m_jours and champ_date == "date_echeance":
            d_emission_norm = champs.get("date_emission")
            if d_emission_norm:
                try:
                    d_emit = datetime.strptime(d_emission_norm[:10], "%Y-%m-%d")
                    return (d_emit + timedelta(days=int(m_jours.group(1)))).strftime("%Y-%m-%d")
                except Exception:
                    pass
        return _normaliser_date(v)

    d_emission = _norm_date("date_emission")
    d_echeance = _norm_date("date_echeance")

    # Fallback dates depuis liste brute
    if not d_emission and len(toutes_dates) >= 1:
        d_emission = toutes_dates[0].strftime("%Y-%m-%d")
    if not d_echeance and len(toutes_dates) >= 2:
        d_echeance = toutes_dates[-1].strftime("%Y-%m-%d")

    champs["date_emission"] = d_emission or ""
    champs["date_echeance"] = d_echeance or ""
    champs["due_date"]      = champs["date_echeance"]
    champs["issue_date"]    = champs["date_emission"]
    champs["drawer"]        = champs.get("tireur", "")
    champs["drawee"]        = champs.get("tire", "")
    champs["draft_number"]  = champs.get("numero_traite", "")

    champs["champs_obligatoires_presents"] = bool(
        champs.get("amount", 0) > 0 and champs.get("date_echeance")
    )
    confiance = sum(scores_confiance) / len(scores_confiance) if scores_confiance else 0.0
    return champs, list(set(incertains)), confiance


# ──────────────────────────────────────────────────────────────────────
# MAPPING VERS FRAPPE
# ──────────────────────────────────────────────────────────────────────

def _mapper_frappe(form_fields: dict, mapping: dict) -> dict:
    champs_remplis = {}
    for cle_ocr, fieldname in mapping.items():
        valeur = form_fields.get(cle_ocr)
        if valeur is None or valeur == "":
            continue
        if isinstance(valeur, float) and valeur == 0.0:
            continue
        champs_remplis[fieldname] = valeur

    if form_fields.get("amount") and form_fields["amount"] > 0:
        champs_remplis["paid_amount"] = form_fields["amount"]

    return champs_remplis


# ──────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE PRINCIPAL
# ──────────────────────────────────────────────────────────────────────

def analyser_document_paiement(
    chemin_img: str,
    texte_ocr: str,
    payment_method: str,
    score_ocr: int = 0,
) -> dict:
    """
    Analyse complète d'un document de paiement (chèque ou traite tunisien).

    Returns:
        {
            "valid": bool,
            "document_type_detected": str,
            "image_enhanced": bool,
            "errors": [...],
            "uncertain_fields": [...],
            "form_fields": dict | None,
            "champs_remplis": dict | None,
            "date_cheque_retenue": str | None,
            "confiance_globale": float,
        }
    """
    erreurs       = []
    incertains    = []
    image_enhanced = False

    texte_ocr = _pretraiter_texte_ocr(texte_ocr or "")

    # Étape 1 : qualité → amélioration si image floue
    if _evaluer_qualite(texte_ocr, score_ocr):
        image_enhanced = True
        txt_ameliore   = _tenter_amelioration_texte(chemin_img)
        if txt_ameliore and len(txt_ameliore.split()) > len(texte_ocr.split()):
            texte_ocr = _pretraiter_texte_ocr(txt_ameliore)

    texte_traite = texte_ocr

    # Étape 2 : détection type
    type_detecte, score_type = _identifier_type_document(texte_traite)

    # Étape 3 : cohérence payment_method
    type_attendu = _normaliser_payment_method(payment_method)
    if (type_detecte != "inconnu" and type_attendu is not None
            and type_detecte != type_attendu):
        label_d = "chèque" if type_detecte == "cheque" else "traite"
        label_a = "chèque" if type_attendu  == "cheque" else "traite"
        erreurs.append(f"Document {label_d} soumis pour un paiement par {label_a}.")
        return _retour_invalide(type_detecte, image_enhanced, erreurs)

    if type_detecte == "inconnu" and type_attendu:
        type_detecte = type_attendu

    # Étape 4 : vérifications métier
    date_cheque_retenue = None

    if type_detecte == "cheque":
        if not _detecter_signature(chemin_img, texte_traite):
            erreurs.append(
                "Chèque non signé ou signature non détectée. "
                "Veuillez soumettre un chèque signé et lisible."
            )
            return _retour_invalide("cheque", image_enhanced, erreurs)

        dates = _extraire_dates_brutes(texte_traite)
        if not dates:
            dates = _extraire_dates_image_cheque(chemin_img)

        if not dates:
            erreurs.append("Date du chèque non détectée.")
            return _retour_invalide("cheque", image_enhanced, erreurs)

        date_doc    = max(dates)
        anciennete  = _anciennete_mois(date_doc)
        if anciennete > PEREMPTION_CHEQUE_MOIS:
            erreurs.append(
                f"Chèque périmé : date {date_doc.strftime('%d/%m/%Y')} — "
                f"ancienneté {int(anciennete)} mois (limite {PEREMPTION_CHEQUE_MOIS} mois)."
            )
            return _retour_invalide("cheque", image_enhanced, erreurs)
        date_cheque_retenue = date_doc.strftime("%Y-%m-%d")

    # Étape 5 : extraction
    if type_detecte == "cheque":
        form_fields, incertains, confiance = _extraire_champs_cheque(texte_traite)
        form_fields["payment_method"]    = "Chèque"
        form_fields["signature_present"] = True
        if date_cheque_retenue and not form_fields.get("cheque_date"):
            form_fields["cheque_date"] = date_cheque_retenue
        if date_cheque_retenue and not form_fields.get("date_cheque"):
            form_fields["date_cheque"] = date_cheque_retenue
        champs_remplis = _mapper_frappe(form_fields, _MAPPING_FRAPPE_CHEQUE)

    elif type_detecte == "traite":
        form_fields, incertains, confiance = _extraire_champs_traite(texte_traite)
        form_fields["payment_method"] = "Traite"
        form_fields["accepted"]       = _detecter_acceptation(texte_traite)
        champs_remplis = _mapper_frappe(form_fields, _MAPPING_FRAPPE_TRAITE)

    else:
        form_fields    = {}
        champs_remplis = {}
        confiance      = 0.0

    return {
        "valid":                  True,
        "document_type_detected": type_detecte,
        "image_enhanced":         image_enhanced,
        "errors":                 erreurs,
        "uncertain_fields":       incertains,
        "form_fields":            form_fields or None,
        "champs_remplis":         champs_remplis or None,
        "date_cheque_retenue":    date_cheque_retenue,
        "confiance_globale":      round(confiance, 3),
        "score_type_document":    round(score_type, 3),
    }


def _retour_invalide(type_doc, image_enhanced, erreurs):
    return {
        "valid":                  False,
        "document_type_detected": type_doc,
        "image_enhanced":         image_enhanced,
        "errors":                 erreurs,
        "uncertain_fields":       [],
        "form_fields":            None,
        "champs_remplis":         None,
        "date_cheque_retenue":    None,
        "confiance_globale":      0.0,
        "score_type_document":    0.0,
    }


# ── Extraction dates image chèque (gardée pour compatibilité) ──────────

def _extraire_dates_image_cheque(chemin_img: str) -> list:
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image as PILImage

        ext = os.path.splitext(chemin_img)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_img, dpi=300)
            if not pages:
                return []
            img = cv2.cvtColor(np.array(pages[0].convert("RGB")), cv2.COLOR_RGB2BGR)
        else:
            img = cv2.imread(chemin_img)
            if img is None:
                pil = PILImage.open(chemin_img).convert("RGB")
                img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

        h, w        = img.shape[:2]
        zones       = [(0.45, 0.00, 1.00, 0.28), (0.00, 0.00, 1.00, 0.35)]
        toutes_dates = []

        for x0p, y0p, x1p, y1p in zones:
            crop = img[int(h*y0p):int(h*y1p), int(w*x0p):int(w*x1p)]
            if crop is None or crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if gray.shape[1] < 1600:
                scale = 1600 / max(gray.shape[1], 1)
                gray  = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            thr = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 12
            )
            for arr in (gray, thr):
                for psm in (6, 7, 11):
                    try:
                        txt = pytesseract.image_to_string(
                            PILImage.fromarray(arr), lang="fra+eng",
                            config=f"--oem 3 --psm {psm}"
                        )
                        if txt and txt.strip():
                            toutes_dates.extend(_extraire_dates_brutes(_pretraiter_texte_ocr(txt)))
                    except Exception:
                        continue
        return list({d.strftime("%Y-%m-%d"): d for d in toutes_dates}.values())
    except Exception:
        return []