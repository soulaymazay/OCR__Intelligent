"""
nlp_analyzer.py — Groupe Bayoudh Metal

Couche NLP légère (sans spaCy ni NLTK) pour enrichir l'analyse OCR.
Utilise rapidfuzz pour la correspondance floue et des heuristiques
contextuelles pour comprendre la nature du document.

Fonctions publiques :
    analyser_contexte(texte, champs_formulaire=None)
        → dict avec :
            type_document, score_type,
            entites  (societes, dates, montants, references),
            champs_enrichis (champ → {valeur, confiance, source}),
            champs_compatibles   [{champ, valeur, fieldname, confiance}]
            champs_incompatibles [{champ, valeur_ocr, valeur_form, raison}]
"""

import re
import unicodedata
from typing import Optional

try:
    from rapidfuzz import fuzz, process as rfprocess
    _RAPIDFUZZ = True
except ImportError:
    _RAPIDFUZZ = False

# ──────────────────────────────────────────────────────────────────────
# VOCABULAIRE DE CONTEXTE
# ──────────────────────────────────────────────────────────────────────

_CONTEXTE = {
    "facture_vente": {
        "mots": [
            "facture de vente",
            "facture client",
        ],
        "poids": 2.0,
    },
    "facture": {
        "mots": [
            "facture", "invoice", "facturation", "à payer", "net à payer",
            "règlement", "tva", "ttc", "ht", "montant ttc", "échéance",
            "facturé", "bill to", "vendu à", "sold to",
        ],
        "poids": 1.0,
    },
    "bon_commande": {
        "mots": [
            "bon de commande", "purchase order", "commande n°", "délai de livraison",
            "veuillez livrer", "conditions de livraison", "po n°", "bc n°",
        ],
        "poids": 1.0,
    },
    "devis": {
        "mots": [
            "devis", "quotation", "proforma", "offre de prix", "valable jusqu",
            "proposition commerciale", "devis n°",
        ],
        "poids": 1.0,
    },
    "bon_livraison": {
        "mots": [
            "bon de livraison", "delivery note", "livré à", "expédié",
            "bordereau d'expédition", "bl n°",
            "bon livraison", "livraison n°", "bordereau de livraison",
            "visa du client", "visa du fournisseur",
            "reçu le", "livré le", "émis par",
            "quantités commandées", "lieu :", "contact client",
            "numéro de client",
        ],
        "poids": 1.0,
    },
}

# Mapping type_document → fieldname Frappe (pour le retour frontend)
_MAPPING_FRAPPE = {
    "facture": {
        "numero_facture": "bill_no",
        "date":           "bill_date",
        "fournisseur":    "supplier",
        "montant_ht":     "net_total",
        "montant_tva":    "total_taxes_and_charges",
        "montant_ttc":    "grand_total",
        "date_echeance":  "due_date",
        "mode_paiement":  "payment_terms_template",
    },
    "bon_commande": {
        "numero_commande": "po_no",
        "date":            "transaction_date",
        "fournisseur":     "supplier",
        "montant_ttc":     "grand_total",
    },
    "devis": {
        "numero_devis":  "quotation_to",
        "date":          "transaction_date",
        "fournisseur":   "supplier",
        "montant_ttc":   "grand_total",
    },
    "bon_livraison": {
        "numero_bl":  "lr_no",
        "date":       "lr_date",
        "fournisseur":"supplier",
    },
    "inconnu": {
        "date":           "posting_date",
        "montant_ttc":    "grand_total",
        "fournisseur":    "supplier",
        "numero_facture": "bill_no",
    },
}

# ──────────────────────────────────────────────────────────────────────
# PATTERNS D'ENTITÉS
# ──────────────────────────────────────────────────────────────────────

_PAT_DATE = re.compile(
    r'\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})\b'
    r'|\b(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})\b'
    r'|\b(\d{1,2})\s+(janvier|f[ée]vrier|mars|avril|mai|juin|juillet'
    r'|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+(\d{4})\b',
    re.IGNORECASE
)

_PAT_MONTANT = re.compile(
    r'([\d\s\u00a0]{2,}(?:[,\.]\d{2,3})?)\s*(?:€|TND|DT|\$|EUR|USD|dinars?)',
    re.IGNORECASE
)

_PAT_MONTANT_IMPLICITE = re.compile(
    r'(?:total|montant|net|ttc|ht|tva)\s*[:\-]?\s*([\d\s\u00a0.,]{4,30})',
    re.IGNORECASE
)

_PAT_REF = re.compile(
    r'\b([A-Z]{2,5}[/\-]?\d{3,}(?:[/\-]\d+)?)\b'
)

_PAT_SOCIETE = re.compile(
    r'\b([A-ZÀÂÉÈÊËÎÏÔÙÛÜ][A-Za-zÀ-ÿ\s&\.\-]{3,50}'
    r'(?:SARL|SA|SPA|SAS|EURL|SNC|CORP|LTD|LLC|INC|GROUP|GROUPE))\b'
    r'|(?:raison sociale|soci[eé]t[eé]|company|fournisseur|vendor)'
    r'\s*[:\-]?\s*([A-ZÀÂÉÈÊËÎÏÔÙÛÜ][^\n]{3,60})',
    re.IGNORECASE
)

_PAT_IBAN = re.compile(
    r'\b([A-Z]{2}\d{2}[\w\s]{10,30}|\d[\d\s]{15,30})\b'
)

_SOCIETE_STOPWORDS = [
    "fodec", "base tva", "tva", "timbre", "taxe", "taxes",
    "montant", "total ht", "total ttc", "total taxe", "remise",
    "designation", "quantite", "article", "reference"
]

# ──────────────────────────────────────────────────────────────────────
# NORMALISATION
# ──────────────────────────────────────────────────────────────────────

def _normaliser(texte: str) -> str:
    """Minuscules, suppression accents pour comparaison."""
    texte = texte.lower()
    texte = unicodedata.normalize("NFD", texte)
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    return texte


def _normaliser_montant(val: str) -> Optional[float]:
    """'5 950,00 TND' → 5950.0"""
    try:
        v = re.sub(r'[^\d,\.]', '', val).replace(",", ".")
        # Gérer le cas "5950.00" et "5.950,00" (séparateur milliers)
        if v.count(".") > 1:
            v = v.replace(".", "", v.count(".") - 1)
        return float(v)
    except Exception:
        return None


def _normaliser_date(val: str) -> str:
    """'15/04/2024' → '15/04/2024', supprimer espaces."""
    return re.sub(r'[\s\u00a0]', '', val)


# ──────────────────────────────────────────────────────────────────────
# DÉTECTION DU TYPE DE DOCUMENT
# ──────────────────────────────────────────────────────────────────────

def _detecter_type(texte: str) -> tuple:
    """
    Retourne (type_document, score_confiance 0.0-1.0).
    Compte les occurrences de mots-clés pondérées.
    """
    texte_norm = _normaliser(texte)
    scores = {}

    for type_doc, config in _CONTEXTE.items():
        hits = 0
        for mot in config["mots"]:
            mot_norm = _normaliser(mot)
            if mot_norm in texte_norm:
                hits += 1
        scores[type_doc] = hits * config["poids"]

    if not scores or max(scores.values()) == 0:
        return "inconnu", 0.0

    best_type  = max(scores, key=scores.get)
    best_score = scores[best_type]
    total      = sum(scores.values())
    confiance  = min(best_score / max(total, 1) * 2, 1.0)

    # Seuil minimal : au moins 1 mot-clé trouvé
    if best_score < 1:
        return "inconnu", 0.0

    return best_type, round(confiance, 3)


# ──────────────────────────────────────────────────────────────────────
# EXTRACTION D'ENTITÉS NOMMÉES (NER léger)
# ──────────────────────────────────────────────────────────────────────

def _extraire_dates(texte: str) -> list:
    """Retourne toutes les dates trouvées dans le texte."""
    dates = []
    for m in _PAT_DATE.finditer(texte):
        dates.append(m.group(0).strip())
    return list(dict.fromkeys(dates))  # dédupliquer en conservant l'ordre


# ──────────────────────────────────────────────────────────────────────
# CONVERSION MONTANT EN LETTRES → NOMBRE
# ──────────────────────────────────────────────────────────────────────

_UNITES = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
    "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
    "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
    "seize": 16, "dix-sept": 17, "dix sept": 17, "dixsept": 17,
    "dix-huit": 18, "dix huit": 18, "dixhuit": 18,
    "dix-neuf": 19, "dix neuf": 19, "dixneuf": 19,
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
    "soixante": 60, "soixante-dix": 70, "soixante dix": 70,
    "quatre-vingt": 80, "quatre vingt": 80, "quatre-vingts": 80, "quatre vingts": 80,
    "quatre-vingt-dix": 90, "quatre vingt dix": 90,
}

_MULTIPLICATEURS = {
    "cent": 100, "cents": 100,
    "mille": 1000,
    "million": 1_000_000, "millions": 1_000_000,
    "milliard": 1_000_000_000, "milliards": 1_000_000_000,
}


def _nombre_en_lettres_vers_chiffre(texte_lettres: str) -> Optional[float]:
    """Convertit un montant en lettres françaises vers un nombre.
    Ex: 'mille sept cent vingt-neuf' → 1729
        'quatre-vingt-six' → 86
    """
    s = texte_lettres.lower().strip()
    # Remplacer les tirets par des espaces, normaliser
    s = re.sub(r'[-—–]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()

    mots = s.split()
    if not mots:
        return None

    total = 0
    courant = 0

    i = 0
    while i < len(mots):
        mot = mots[i]

        # Gestion "soixante dix", "quatre vingt", "quatre vingt dix"
        bigramme = mot + " " + mots[i + 1] if i + 1 < len(mots) else ""
        trigramme = bigramme + " " + mots[i + 2] if i + 2 < len(mots) else ""

        if trigramme in _UNITES:
            courant += _UNITES[trigramme]
            i += 3
        elif bigramme in _UNITES:
            courant += _UNITES[bigramme]
            i += 2
        elif mot in _UNITES:
            courant += _UNITES[mot]
            i += 1
        elif mot in ("et",):
            i += 1
        elif mot in _MULTIPLICATEURS:
            mult = _MULTIPLICATEURS[mot]
            if mult == 100:
                courant = (courant if courant > 0 else 1) * 100
            elif mult == 1000:
                courant = (courant if courant > 0 else 1) * 1000
                total += courant
                courant = 0
            else:
                courant = (courant if courant > 0 else 1) * mult
                total += courant
                courant = 0
            i += 1
        else:
            i += 1

    total += courant
    return total if total > 0 else None


def _extraire_montant_en_lettres(texte: str) -> Optional[float]:
    """Cherche un montant en lettres dans le texte OCR.
    Patterns : 'la somme de ... dinars et ... millimes'
    """
    texte_lower = texte.lower()
    # Normaliser les erreurs OCR courantes
    texte_lower = texte_lower.replace("\n", " ").replace("  ", " ")

    # Pattern : "somme de ... dinars (et ... millimes)"
    m = re.search(
        r'(?:somme\s+(?:de|ae)\s+)(.*?)(?:dinars?)',
        texte_lower, re.IGNORECASE
    )
    if not m:
        # Pattern alternatif : "montant ... dinars"
        m = re.search(
            r'(?:montant\s+(?:de\s+)?)(.*?)(?:dinars?)',
            texte_lower, re.IGNORECASE
        )

    if not m:
        return None

    partie_dinars = m.group(1).strip()
    dinars = _nombre_en_lettres_vers_chiffre(partie_dinars)
    if dinars is None:
        return None

    # Seuil minimum : une facture réelle fait au moins 100 TND
    # Un montant < 100 est très probablement un faux positif OCR
    if dinars < 100:
        return None

    # Chercher les millimes après "et ... millimes"
    millimes = 0
    m2 = re.search(
        r'(?:dinars?\s+et\s+)(.*?)(?:millimes?)',
        texte_lower, re.IGNORECASE
    )
    if m2:
        partie_millimes = m2.group(1).strip()
        v = _nombre_en_lettres_vers_chiffre(partie_millimes)
        if v is not None:
            millimes = v

    return dinars + millimes / 1000.0


def _extraire_montants(texte: str) -> list:
    """Retourne tous les montants trouvés."""
    montants = []
    for m in _PAT_MONTANT.finditer(texte):
        v = m.group(1).strip()
        if re.search(r'\d{2,}', v):
            montants.append(v)
    for m in _PAT_MONTANT_IMPLICITE.finditer(texte):
        v = m.group(1).strip()
        if re.search(r'\d{3,}', v) and v not in montants:
            montants.append(v)

    # Tentative d'extraction de montant en lettres (français/tunisien)
    montant_lettres = _extraire_montant_en_lettres(texte)
    if montant_lettres is not None:
        montants.append(str(montant_lettres))

    return list(dict.fromkeys(montants))


def _extraire_references(texte: str) -> list:
    refs = []
    for m in _PAT_REF.finditer(texte):
        refs.append(m.group(0))
    return list(dict.fromkeys(refs))


def _societe_valide(valeur: str) -> bool:
    v = (valeur or "").strip()
    if len(v) < 3:
        return False

    n = _normaliser(v)
    if any(w in n for w in _SOCIETE_STOPWORDS):
        return False

    # Évite les lignes presque totalement numériques
    if re.fullmatch(r'[\d\s\W_]+', v):
        return False

    return True


def _extraire_societes(texte: str) -> list:
    societes = []
    for m in _PAT_SOCIETE.finditer(texte):
        v = (m.group(1) or m.group(2) or "").strip()
        if _societe_valide(v):
            societes.append(v)

    # Fallback : chercher une entête en majuscules dans les premières lignes
    if not societes:
        for line in texte.splitlines()[:30]:
            cand = re.sub(r'\s+', ' ', line).strip(" -:|\t")
            if not cand:
                continue
            # Ex: FORTISTORE
            if re.fullmatch(r'[A-Z0-9&\-\s]{4,40}', cand) and _societe_valide(cand):
                societes.append(cand)

    # Nettoyer : supprimer les préfixes d'une seule lettre (ex: "A FORTISTORE" → "FORTISTORE")
    cleaned = []
    for s in societes:
        s = re.sub(r'^[A-Za-z]\s+', '', s).strip()
        if _societe_valide(s):
            cleaned.append(s)

    return list(dict.fromkeys(cleaned))


# ──────────────────────────────────────────────────────────────────────
# ENRICHISSEMENT DE CHAMPS PAR NLP
# ──────────────────────────────────────────────────────────────────────

def _enrichir_champs(champs_regex: dict, entites: dict, texte: str, type_doc: str) -> dict:
    """
    Prend les champs extraits par regex et les enrichit :
    - Si un champ est absent, tente de le déduire depuis les entités
    - Calcule un score de confiance par champ
    """
    enrichis = {}
    texte_norm = texte.lower()

    # Copier les champs regex avec confiance élevée
    for champ, valeur in champs_regex.items():
        if valeur and str(valeur).strip():
            enrichis[champ] = {
                "valeur":    str(valeur).strip(),
                "confiance": _calculer_confiance_champ(champ, str(valeur), texte_norm),
                "source":    "regex",
            }

    # ── Compléter depuis les entités NER ─────────────────────────────

    # Fournisseur : si absent ou confiance basse, essayer les sociétés NER
    if "fournisseur" not in enrichis or enrichis["fournisseur"]["confiance"] < 0.5:
        societes = entites.get("societes", [])
        if societes:
            enrichis["fournisseur"] = {
                "valeur":    societes[0],
                "confiance": 0.5,
                "source":    "nlp_ner",
            }

    # Date principale : prendre la première date si absente
    if "date" not in enrichis:
        dates = entites.get("dates", [])
        if dates:
            enrichis["date"] = {
                "valeur":    dates[0],
                "confiance": 0.55,
                "source":    "nlp_ner",
            }

    # Montant TTC : depuis entités si absent
    if "montant_ttc" not in enrichis:
        montants = entites.get("montants", [])
        if montants:
            # Prendre le plus grand montant comme TTC
            candidats = []
            for m in montants:
                v = _normaliser_montant(m)
                if v is not None:
                    candidats.append((v, m))
            if candidats:
                candidats.sort(key=lambda x: x[0], reverse=True)
                enrichis["montant_ttc"] = {
                    "valeur":    candidats[0][1],
                    "confiance": 0.4,
                    "source":    "nlp_ner",
                }

    # Numéro de référence principal : depuis les refs si absent
    ref_champ = {
        "facture":      "numero_facture",
        "bon_commande": "numero_commande",
        "devis":        "numero_devis",
        "bon_livraison":"numero_bl",
    }.get(type_doc)

    if ref_champ and ref_champ not in enrichis:
        refs = entites.get("references", [])
        if refs:
            enrichis[ref_champ] = {
                "valeur":    refs[0],
                "confiance": 0.4,
                "source":    "nlp_ner",
            }

    return enrichis


def _calculer_confiance_champ(champ: str, valeur: str, texte_lower: str) -> float:
    """
    Score de confiance pour un champ extrait par regex :
    0.0 → absent du texte  |  1.0 → valeur présente avec bon format
    """
    if not valeur:
        return 0.0

    score = 0.0
    val_lower = valeur.lower()

    # Présence dans le texte brut
    if val_lower in texte_lower:
        score += 0.5
    elif _RAPIDFUZZ:
        # Matching flou si pas de correspondance exacte
        ratio = fuzz.partial_ratio(val_lower, texte_lower) / 100.0
        score += 0.5 * min(ratio, 0.9)

    # Validation de format
    est_date    = re.search(r'\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}', valeur)
    est_montant = re.search(r'\d[\d\s.,]{2,}', valeur)
    est_ref     = re.search(r'[A-Z0-9\-/]{3,}', valeur, re.IGNORECASE)

    if "date" in champ and est_date:
        score += 0.3
    elif any(k in champ for k in ["montant", "total", "ttc", "ht", "tva"]) and est_montant:
        score += 0.3
    elif any(k in champ for k in ["numero", "reference", "ref"]) and est_ref:
        score += 0.3
    elif "fournisseur" in champ or "client" in champ:
        if len(valeur) > 3:
            score += 0.2

    return round(min(score, 1.0), 3)


# ──────────────────────────────────────────────────────────────────────
# SCORE DE COMPATIBILITÉ CHAMP ↔ FORMULAIRE (avec NLP)
# ──────────────────────────────────────────────────────────────────────

def _compatibilite_champ(champ: str, valeur_ocr: str, valeur_form: str) -> dict:
    """
    Compare une valeur OCR avec la valeur du formulaire.

    Retourne:
        {"compatible": True/False, "score": 0.0-1.0, "raison": str}
    """
    if not valeur_form or str(valeur_form).strip() == "" or str(valeur_form) == "None":
        # Champ vide dans le formulaire → neutre
        return {"compatible": True, "score": 1.0, "raison": "champ vide dans formulaire"}

    v_ocr  = str(valeur_ocr).strip()
    v_form = str(valeur_form).strip()
    est_montant = any(k in champ for k in ["montant", "total", "ttc", "ht", "tva", "paid"])
    est_date    = "date" in champ

    # ── Comparaison montants ────────────────────────────────────────
    if est_montant:
        n_ocr  = _normaliser_montant(v_ocr)
        n_form = _normaliser_montant(v_form)
        if n_ocr is not None and n_form is not None:
            diff = abs(n_ocr - n_form)
            if diff < 0.02:
                return {"compatible": True,  "score": 1.0,
                        "raison": f"montant identique ({v_ocr})"}
            elif diff / max(n_form, 1) < 0.05:
                return {"compatible": True,  "score": 0.8,
                        "raison": f"montant approché (diff {diff:.2f})"}
            else:
                return {"compatible": False, "score": 0.1,
                        "raison": f"montant différent : OCR={v_ocr}, formulaire={v_form}"}

    # ── Comparaison dates ────────────────────────────────────────────
    if est_date:
        d_ocr  = _normaliser_date(v_ocr)
        d_form = _normaliser_date(v_form)
        if d_ocr == d_form:
            return {"compatible": True,  "score": 1.0,  "raison": "date identique"}
        if d_ocr[:8] == d_form[:8]:
            return {"compatible": True,  "score": 0.85, "raison": "date approchée"}
        # Vérifier si les composantes sont présentes
        parties_ocr  = set(re.split(r'[/\-\.]', d_ocr))
        parties_form = set(re.split(r'[/\-\.]', d_form))
        communes = len(parties_ocr & parties_form)
        if communes >= 2:
            return {"compatible": True,  "score": 0.65,
                    "raison": f"date partiellement compatible ({communes}/3 composantes)"}
        return {"compatible": False, "score": 0.1,
                "raison": f"date différente : OCR={v_ocr}, formulaire={v_form}"}

    # ── Comparaison texte / références ────────────────────────────────
    v_ocr_n  = _normaliser(v_ocr)
    v_form_n = _normaliser(v_form)

    if v_ocr_n == v_form_n:
        return {"compatible": True,  "score": 1.0, "raison": "valeur identique"}
    if v_ocr_n in v_form_n or v_form_n in v_ocr_n:
        return {"compatible": True,  "score": 0.8, "raison": "valeur incluse"}

    if _RAPIDFUZZ:
        ratio = fuzz.token_set_ratio(v_ocr_n, v_form_n) / 100.0
        if ratio >= 0.80:
            return {"compatible": True,  "score": ratio,
                    "raison": f"similarité floue {int(ratio*100)}%"}
        elif ratio >= 0.55:
            return {"compatible": True,  "score": ratio,
                    "raison": f"similarité partielle {int(ratio*100)}%"}
        else:
            return {"compatible": False, "score": ratio,
                    "raison": f"valeurs différentes (similarité {int(ratio*100)}%) : "
                              f"OCR=« {v_ocr} », formulaire=« {v_form} »"}
    else:
        # Fallback sans rapidfuzz : vérification mot par mot
        mots_ocr  = set(v_ocr_n.split())
        mots_form = set(v_form_n.split())
        communes  = len(mots_ocr & mots_form)
        total     = max(len(mots_ocr | mots_form), 1)
        ratio     = communes / total
        if ratio >= 0.6:
            return {"compatible": True,  "score": ratio, "raison": "mots communs"}
        return {"compatible": False, "score": ratio,
                "raison": f"valeurs incompatibles : OCR=« {v_ocr} » ≠ formulaire=« {v_form} »"}


# ──────────────────────────────────────────────────────────────────────
# FONCTION PRINCIPALE
# ──────────────────────────────────────────────────────────────────────

def analyser_contexte(texte: str, champs_regex: dict = None, champs_formulaire: dict = None,
                      type_doc_force: str = None) -> dict:
    """
    Analyse NLP complète du texte OCR.

    Paramètres :
        texte            : texte extrait par OCR
        champs_regex     : champs déjà extraits par header_extractor (dict)
        champs_formulaire: valeurs actuelles du formulaire Frappe (dict fieldname→valeur)
        type_doc_force   : forcer le type de document si déjà connu

    Retourne :
    {
        "type_document"       : str,
        "score_type"          : float,
        "entites"             : {"societes", "dates", "montants", "references"},
        "champs_enrichis"     : {champ: {"valeur", "confiance", "source"}},
        "champs_compatibles"  : [{champ, valeur, fieldname, confiance, score_compat}],
        "champs_incompatibles": [{champ, valeur_ocr, valeur_form, raison}],
        "nb_compatibles"      : int,
        "nb_incompatibles"    : int,
        "suggestion"          : str  (message à afficher à l'utilisateur),
    }
    """
    champs_regex     = champs_regex     or {}
    champs_formulaire = champs_formulaire or {}

    # ── 1. Détection du type de document ─────────────────────────────
    if type_doc_force and type_doc_force != "inconnu":
        type_doc   = type_doc_force
        score_type = 0.9
    else:
        type_doc, score_type = _detecter_type(texte)

    # ── 2. Extraction d'entités NER ──────────────────────────────────
    entites = {
        "societes":   _extraire_societes(texte),
        "dates":      _extraire_dates(texte),
        "montants":   _extraire_montants(texte),
        "references": _extraire_references(texte),
    }

    # ── 3. Enrichissement des champs ─────────────────────────────────
    champs_enrichis = _enrichir_champs(champs_regex, entites, texte, type_doc)

    # ── 4. Comparaison champs enrichis ↔ formulaire Frappe ───────────
    mapping_frappe = _MAPPING_FRAPPE.get(type_doc, _MAPPING_FRAPPE["inconnu"])

    champs_compatibles   = []
    champs_incompatibles = []

    for champ_ocr, meta in champs_enrichis.items():
        valeur_ocr = meta["valeur"]
        fieldname  = mapping_frappe.get(champ_ocr)

        if not fieldname:
            continue

        valeur_form = champs_formulaire.get(fieldname)
        compat      = _compatibilite_champ(champ_ocr, valeur_ocr, valeur_form)

        entry = {
            "champ":     champ_ocr,
            "fieldname": fieldname,
            "valeur_ocr": valeur_ocr,
            "confiance_extraction": meta["confiance"],
            "source":    meta.get("source", "regex"),
        }

        if compat["compatible"]:
            champs_compatibles.append({
                **entry,
                "score_compat": compat["score"],
                "raison":       compat["raison"],
            })
        else:
            champs_incompatibles.append({
                **entry,
                "valeur_form":  str(valeur_form or ""),
                "score_compat": compat["score"],
                "raison":       compat["raison"],
            })

    # ── 5. Suggestion contextuelle ────────────────────────────────────
    if champs_incompatibles:
        champs_pb = ", ".join(
            f"« {e['champ']} »" for e in champs_incompatibles[:3]
        )
        suggestion = (
            f"Le document de type « {type_doc} » présente {len(champs_incompatibles)} "
            f"champ(s) incompatible(s) avec le formulaire : {champs_pb}. "
            "Vérifiez que vous avez téléversé le bon document."
        )
    elif len(champs_compatibles) >= 3:
        suggestion = (
            f"Document identifié comme « {type_doc} » avec "
            f"{len(champs_compatibles)} champ(s) compatible(s). "
            "Formulaire prêt à être rempli."
        )
    elif len(champs_enrichis) == 0:
        suggestion = (
            "Aucun champ n'a pu être extrait. "
            "Vérifiez la qualité du document (résolution min. 300 DPI)."
        )
    else:
        suggestion = (
            f"Document « {type_doc} » analysé mais peu de champs compatibles "
            f"({len(champs_compatibles)}/{len(champs_enrichis)}). "
            "Vérifiez le document et complétez manuellement."
        )

    return {
        "type_document":        type_doc,
        "score_type":           score_type,
        "entites":              entites,
        "champs_enrichis":      champs_enrichis,
        "champs_compatibles":   champs_compatibles,
        "champs_incompatibles": champs_incompatibles,
        "nb_compatibles":       len(champs_compatibles),
        "nb_incompatibles":     len(champs_incompatibles),
        "suggestion":           suggestion,
    }