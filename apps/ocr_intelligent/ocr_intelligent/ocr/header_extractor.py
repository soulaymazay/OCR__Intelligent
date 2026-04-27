"""
header_extractor.py — Groupe Bayoudh Metal
Lecture des en-têtes du document OCR.

Retourne :
{
    "type_document"   : "facture" | "bon_commande" | "devis" | "inconnu",
    "champs"          : { champ: valeur | None },   ← tous les champs
    "champs_trouves"  : ["numero_facture", ...],    ← champs avec valeur
    "champs_manquants": ["montant_ht", ...]         ← champs vides (None)
}

CORRECTIONS v2 :
  - numero_facture : capture désormais les formats alphanumériques complets
    (ex: FAC-2024-00142, FACT/2024/001, INV-001, etc.)
  - montant_ht / tva / montant_ttc : patterns élargis pour "Montant HT :",
    "TVA (19%) :", "Montant TTC :" tels qu'on les trouve sur les factures TN
  - Ajout pattern "Montant HT :" et "Montant TTC :" en tête de pattern list
  - echeance : ajoute alias "Date Échéance" / "Date Echeance"
"""

import re


# ─────────────────────────────────────────────────────────────────────
# DÉTECTION DU TYPE DE DOCUMENT
# ─────────────────────────────────────────────────────────────────────

TYPE_PATTERNS = {
    "facture": [
        r"\bfacture\b", r"\binvoice\b", r"\bfact\s*n[°o]", r"\bfac[-\s]?\d+\b"
    ],
    "bon_commande": [
        r"\bbon\s+de\s+commande\b", r"\bpurchase\s+order\b", r"\bbc[-\s]?\d+\b", r"\bpo[-\s]?\d+\b"
    ],
    "devis": [
        r"\bdevis\b", r"\bquotation\b", r"\bproforma\b", r"\boffre\s+de\s+prix\b"
    ],
    "bon_livraison": [
        r"\bbon\s+de\s+livraison\b", r"\bdelivery\s+note\b", r"\bbl[-\s]?\d+\b",
        r"\bbon\s*livraison\b",
        r"\blivraison\s+n[°o]\b",
        r"\bbordereau\s+de\s+livraison\b",
        r"\bvisa\s+du\s+(?:client|fournisseur)\b",
        r"\blivr[eé]\s+le\b",
        r"\bre[çc]u\s+le\b",
        r"\b[eé]mis\s+par\b",
        r"\blieu\s*[:\-]",
        r"\bcontact\s+client\b",
        r"\bnuméro\s+de\s+client\b",
        r"\bquantit[eé]s?\s+command[eé]es?\b",
    ],
    "facture_vente": [
        r"\bfacture\s+de\s+vente\b",
        r"\bfacture\s+client\b",
        r"\bfacture\s+(?:de\s+)?v(?:ente)?[-\s]?\d*\b",
    ],
    "cheque": [
        r"\bch[eè]que\b", r"\bpayez\s+contre\b", r"\bpayable\s+[àa]\b",
        r"\bcinq\s+mille\b", r"\bmille\s+euro\b", r"\bbillet\b",
        r"\bvirement\b", r"\border\s+of\b", r"\bpay\s+to\b",
    ],
    "traite": [
        r"\blettre\s*de\s*change\b",
        r"\btraite\b",
        r"\beffet\s*de\s*commerce\b",
        r"\btireur\b",
        r"\btiré\b",
        r"\bdomiciliataire\b",
        r"\bvaleur\s+en\s+compte\b",
        r"\bvaleur\s+re[çc]ue\b",
        r"\baval\b",
        r"\b[àa]\s+vue\b",
        r"\bbon\s*pour\s*aval\b",
        r"\bveuillez\s+payer\b",
    ],
}

# ─────────────────────────────────────────────────────────────────────
# CHAMPS PAR TYPE DE DOCUMENT
# ─────────────────────────────────────────────────────────────────────

CHAMPS_PAR_TYPE = {
    "facture":      ["numero_facture", "date", "echeance", "fournisseur", "client",
                     "reference_commande", "montant_ht", "tva", "montant_ttc",
                     "mode_paiement", "rib_iban"],
    "bon_commande": ["numero_commande", "date", "fournisseur", "client",
                     "reference_article", "quantite", "prix_unitaire",
                     "montant_ht", "tva", "montant_ttc", "delai_livraison"],
    "devis":        ["numero_devis", "date", "validite", "fournisseur", "client",
                     "reference_article", "quantite", "prix_unitaire",
                     "montant_ht", "tva", "montant_ttc"],
    "bon_livraison":["numero_bl", "date", "fournisseur", "client",
                     "reference_commande", "reference_article", "quantite"],
    "cheque":       ["numero_cheque", "date_cheque", "montant", "banque",
                     "titulaire_compte", "rib", "beneficiaire", "memo"],
    "traite":       ["numero_traite", "montant", "date_emission", "date_echeance",
                     "tireur", "tire", "beneficiaire", "domiciliataire", "banque"],
    "inconnu":      ["date", "fournisseur", "client", "montant_ht", "tva", "montant_ttc"],
}

# ─────────────────────────────────────────────────────────────────────
# PATTERNS D'EXTRACTION PAR CHAMP
# ─────────────────────────────────────────────────────────────────────

CHAMP_PATTERNS = {

    # ── Numéros de documents ──────────────────────────────────────────
    # CORRECTION : les patterns sont ordonnés du plus spécifique au plus général.
    # On capture maintenant les formats alphanumériques complets :
    #   FAC-2024-00142, FACT/2024/001, INV-0042, F-001, etc.
    "numero_facture": [
        # Format "Numéro Facture : FAC-2024-00142" (label explicite)
        r"(?:num[eé]ro\s*(?:de\s*)?facture|n[°o]\s*(?:de\s*)?facture)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,25})",
        # Format "Facture N° : FAC-2024-00142"
        r"(?:facture|invoice)\s*n[°o]?\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,25})(?=\s|$|[,;.|])",
        # Format "N° : FAC-2024-00142" (label court) — exclure dates pures
        r"\bN[°o]\s*[:\-]\s*([A-Za-z][A-Za-z0-9\-/]{2,25})",
        # Format code alphanum seul : FAC-XXXX-YYYYY / FACT-XXXX / F-XXX
        r"\b((?:FAC|FACT|INV|FACTURE|BILL|FC)[/\-]?\d{2,4}[/\-]?\d{0,6})\b",
        # Fallback numérique pur si tout le reste échoue
        r"(?:facture|invoice)\s*n[°o]?\s*[:\-]?\s*(\d{1,10})",
        r"\bfac[-\s]?(\d{2,})\b",
        # N° seul suivi d'un nombre (en dernier recours)
        r"(?:^|\s)N[°o]\s*[:\-]?\s*(\d{1,10})(?:\s|$)",
    ],
    "numero_commande": [
        r"(?:bon\s*de\s*commande|purchase\s*order|commande)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\b(?:bc|po)[-\s]?(\d{3,})\b",
    ],
    "numero_devis": [
        r"(?:devis|quotation|offre)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\bdev[-\s]?(\d{3,})\b",
    ],
    "numero_bl": [
        r"(?:bon\s*de\s*livraison|delivery\s*note)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\bbl[-\s]?(\d{3,})\b",
    ],

    # ── Dates ─────────────────────────────────────────────────────────
    "date": [
        # Date facture explicite (prioritaire)
        r"(?:date\s*(?:de\s*)?(?:facture|[eé]mission|document)?)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        # Date simple, en excluant les labels d'échéance
        r"\bdate(?!\s*d['’]?\s*[eé]ch[eé]ance)(?!\s*[eé]ch[eé]ance)(?!\s*limite)\s*[:\-]\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",
        r"(\d{1,2}\s+(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+\d{4})",
    ],
    "echeance": [
        # CORRECTION : ajout de "Date Échéance" / "Date Echeance" (format facture TN standard)
        r"(?:date\s*d['’]?\s*[eé]ch[eé]ance|date\s*[eé]ch[eé]ance|[eé]ch[eé]ance|date\s*limite|due\s*date|payer\s*avant)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        # OCR bruité : nombre placé avant le label (ex: "15/04/2024 Date Echeance")
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\s*(?:date\s*d['’]?\s*[eé]ch[eé]ance|date\s*[eé]ch[eé]ance|[eé]ch[eé]ance|date\s*limite|due\s*date)",
    ],
    "validite": [
        r"(?:valide?\s*(?:jusqu[''au]*)?|validity|expir[ae])\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ],
    "delai_livraison": [
        r"(?:délai|delai|livraison\s*(?:prévue)?|delivery\s*date)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d+\s*(?:jours?|semaines?|days?))",
    ],

    # ── Parties ───────────────────────────────────────────────────────
    "fournisseur": [
        r"(?:fournisseur|vendor|supplier|émis\s*par|issued\s*by|de\s*la\s*part\s*de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
        r"(?:raison\s*sociale|soci[ée]t[ée]|company\s*name|entreprise)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
    ],
    "client": [
        r"(?:client|customer|bill\s*to|facturer\s*[àa]|destinataire|adresser\s*[àa])\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
        r"(?:vendu\s*[àa]|sold\s*to)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
    ],

    # ── Références ────────────────────────────────────────────────────
    "reference_commande": [
        r"(?:référence\s*commande|ref\.?\s*commande|order\s*ref\.?|bc\s*n[°o]?)\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"(?:votre\s*(?:référence|commande))\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
    ],
    "reference_article": [
        r"(?:référence\s*article|ref\.?\s*article|part\s*n[°o]?|article\s*n[°o]?)\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
    ],

    # ── Quantités et prix ─────────────────────────────────────────────
    "quantite": [
        r"(?:quantité|qté|qty|quantity)\s*[:\-]?\s*(\d+(?:[.,]\d+)?(?:\s*(?:pcs?|unités?|kg|m|l))?)",
    ],
    "prix_unitaire": [
        r"(?:prix\s*unitaire|p\.?\s*u\.?|unit\s*price)\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT)?)",
    ],

    # ── Montants ──────────────────────────────────────────────────────
    # CORRECTION : ajout de patterns pour "Montant HT :", "Total HT :" et
    #              formats tunisiens avec DT / TND en fin de ligne.
    #              Chaque pattern tente de capturer uniquement le nombre (sans l'unité).
    "montant_ht": [
        # "Montant HT : 5 950.000 DT" ou "Montant HT : 5 950,000 DT"
        r"(?:montant\s*h\.?t\.?)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
        # "Total HT : ..."
        r"(?:total\s*h\.?t\.?|hors\s*taxe|sous[-\s]?total|subtotal|net\s*amount)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
        # "Base imposable ..."
        r"(?:base\s*imposable|net\s*ht)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
    ],
    "tva": [
        # "TVA (19%) : 1 130.500 DT"
        r"(?:tva|t\.v\.a\.?|vat)\s*(?:\(?\s*\d+[\s.,]*%\s*\)?)?\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
        # "Taxe ..."
        r"(?:taxe)\s*(?:\(?\s*\d+[\s.,]*%\s*\)?)?\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
    ],
    "montant_ttc": [
        # "Montant TTC : 7 080.500 DT"
        r"(?:montant\s*t\.?t\.?c\.?)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
        # "Total TTC / net à payer / amount due"
        r"(?:total\s*t\.?t\.?c\.?|total\s*(?:à\s*payer|due|general)|net\s*à\s*payer|amount\s*due)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:€|\$|TND|DT))?)",
        # Dernier recours : "Total : 7 080.500 DT"  (mais jamais avant les patterns ci-dessus)
        r"(?:total)\s*[:\-]\s*([\d\s.,]+\s*(?:€|\$|TND|DT))",
    ],

    # ── Paiement ──────────────────────────────────────────────────────
    "mode_paiement": [
        r"(?:mode\s*(?:de\s*)?paiement|payment\s*method|règlement|payment\s*terms)\s*[:\-]?\s*([^\n]{3,40})",
    ],
    "rib_iban": [
        r"(?:rib|iban|compte\s*bancaire|bank\s*account)\s*[:\-]?\s*([A-Z]{2}\d{2}[\w\s]{10,30}|\d[\d\s]{15,30})",
    ],

    # ── Chèque ────────────────────────────────────────────────────────
    "numero_cheque": [
        r"(?:n[°o]?\s*(?:du\s*)?ch[eè]que|ch[eè]que\s*n[°o]?)\s*[:\-]?\s*([0-9]{4,12})",
        r"(?:numéro|num\.?|ref\.?)\s*ch[eè]que\s*[:\-]?\s*([0-9]{4,12})",
        r"\b(0[0-9]{5,11})\b",
    ],
    "date_cheque": [
        r"(?:date\s*(?:du\s*ch[eè]que)?|tunis\s*,?\s*le|\ble\b)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",
    ],
    "titulaire_compte": [
        r"(?:titulaire|account\s*holder|compte\s*de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
        r"(?:tiré\s+sur\s+le\s+compte\s+de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
    ],
    "beneficiaire": [
        r"(?:[àa]\s+l[''']ordre\s+de|pay(?:able)?\s+to|bénéficiaire|ordre\s+de)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
        r"(?:payer\s+[àa]|veuillez\s+payer\s+[àa])\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
    ],
    "montant": [
        r"(?:montant|somme(?:\s+de)?|la\s+somme\s+de|sum\s+of|amount)\s*[:\-]?\s*([\d\s.,]+(?:\s*(?:TND|DT|dinars?|euros?|€))?)",
        r"\*+([\d\s.,]+)\*+",
        r"([\d\s.,]{4,})\s*(?:TND|DT|dinars?)",
    ],
    "banque": [
        r"(?:banque|bank|établissement|tiré\s+sur)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{2,50})",
        r"\b(STB|BNA|BIAT|UIB|ATB|BT|BH|Zitouna|QNB|UBCI|ABC|CIB|BFT|Attijari|Wafa|Amen)\b",
    ],
    "rib": [
        r"(?:rib|compte)\s*[:\-]?\s*(\d[\d\s]{14,29})",
        r"(\d{2}\s*\d{3}\s*\d{3,5}\s*\d{3,13}\s*\d{2})",
    ],
    "memo": [
        r"(?:objet|memo|motif|pour|réf\.?|ref\.?)\s*[:\-]?\s*([^\n]{3,80})",
    ],

    # ── Traite (Lettre de Change) ──────────────────────────────────────
    "numero_traite": [
        r"(?:n[°o]?\s*(?:de\s*(?:la\s*)?)?traite|traite\s*n[°o]?|n[°o]?\s*(?:lettre|effet))\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"(?:référence|réf\.?|ref\.?)\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\b(T[-\s]?\d{4}[-\s]\d{2,6})\b",
    ],
    "date_emission": [
        r"(?:date\s*d[''']?[eé]mission|émis(?:e)?\s*le|date\s*(?:du\s*)?document|fait\s*le)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ],
    "date_echeance": [
        r"(?:date\s*d[''']?éch[eé]ance|éch[eé]ance|due\s*date|payable\s*le|à\s*payer\s*le)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:écheance|echeance)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ],
    "tireur": [
        r"(?:tireur|drawer|émis\s*par|souscripteur)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
    ],
    "tire": [
        r"(?:tiré|drawee|débiteur)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,70})",
        r"\b(STB|BNA|BIAT|UIB|ATB|BT|BH|Zitouna|QNB|UBCI|ABC|CIB|BFT|Attijari|Wafa|Amen)\b",
    ],
    "domiciliataire": [
        r"(?:domiciliataire|domiciliation|banque\s+dom\.?)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
        r"(?:agence)\s*[:\-]?\s*([A-Za-zÀ-ü][^\n]{3,60})",
    ],
}


# ─────────────────────────────────────────────────────────────────────
# FONCTIONS PRINCIPALES
# ─────────────────────────────────────────────────────────────────────

def detecter_type_document(texte: str) -> str:
    """Détecte le type du document à partir du texte OCR."""
    texte_lower = texte.lower()
    scores = {t: 0 for t in TYPE_PATTERNS}

    for type_doc, patterns in TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, texte_lower, re.IGNORECASE):
                scores[type_doc] += 1

    meilleur = max(scores, key=scores.get)
    return meilleur if scores[meilleur] > 0 else "inconnu"


def _est_faux_positif_partie(valeur: str) -> bool:
    v = (valeur or "").strip().lower()
    if not v:
        return True

    mots_interdits = [
        "fodec", "base tva", "tva", "timbre", "taxe", "taxes",
        "total ht", "total ttc", "total taxe", "montant", "remise",
        "article", "quantite", "designation"
    ]
    if any(m in v for m in mots_interdits):
        return True

    if re.fullmatch(r"[\d\s\W_]+", v):
        return True

    return False


_STOP_PARTIE_RE = re.compile(
    r'\s*[|/\\]'
    r'|\s+\d{1,2}[\s]*/[\s]*\d{1,2}[\s]*/[\s]*\d{2,4}'
    r'|\s+\d{1,2}[-.]\d{1,2}[-.]\d{2,4}'
    r'|\s*,\s*(?:Tunis|Sfax|Sousse|Monastir|Bizerte|Nabeul|Kairouan|Gab[eè]s|Ariana|Ben\s*Arous)'
    r'|\s+(?:Zone|Avenue|Rue|Route|Blvd|Boulevard|BP\b|Tel|T[eé]l|Fax|Email|Lot\b|Facture\b|Date\b)',
    re.IGNORECASE
)


def _nettoyer_nom_partie(valeur: str) -> str:
    m = _STOP_PARTIE_RE.search(valeur)
    if m:
        valeur = valeur[:m.start()]
    m_form = re.search(
        r'\b(?:SARL|SA\b|SUARL|SAS\b|EURL|SNC|GIE|CORP|LTD|LLC|INC)\b',
        valeur, re.IGNORECASE
    )
    if m_form:
        valeur = valeur[:m_form.end()]
    return valeur.strip(" .,;:|")


def _nettoyer_montant(valeur: str) -> str:
    """
    Nettoie une valeur de montant extraite par OCR :
    - Supprime le suffixe unité (DT, TND, €, $)
    - Normalise les espaces
    CORRECTION : évite de supprimer les chiffres significatifs
    """
    if not valeur:
        return valeur
    # Supprimer les unités en fin de chaîne
    valeur = re.sub(r'\s*(?:DT|TND|€|\$|EUR)\s*$', '', valeur.strip(), flags=re.IGNORECASE)
    return valeur.strip()


def extraire_champ(texte: str, champ: str) -> str | None:
    """
    Extrait un champ précis du texte.
    Retourne la valeur nettoyée, ou None si non trouvé.
    """
    patterns = CHAMP_PATTERNS.get(champ, [])

    for pattern in patterns:
        match = re.search(pattern, texte, re.IGNORECASE | re.MULTILINE)
        if match:
            # Empêche la date d'échéance de remplir le champ date facture
            if champ == "date":
                line_start = texte.rfind("\n", 0, match.start()) + 1
                line_end = texte.find("\n", match.start())
                if line_end == -1:
                    line_end = len(texte)
                ligne = texte[line_start:line_end].lower()
                if re.search(r"[eé]ch[eé]ance|due\s*date|date\s*limite|payer\s*avant", ligne):
                    continue

            valeur = match.group(1).strip()
            valeur = re.sub(r'\s+', ' ', valeur)
            valeur = valeur.strip(" .,;:")

            # Anti faux positifs sur fournisseur/client
            if champ in {"fournisseur", "client"} and _est_faux_positif_partie(valeur):
                continue

            # Nettoyer les noms de parties
            if champ in {"fournisseur", "client"}:
                valeur = _nettoyer_nom_partie(valeur)
                if not valeur or len(valeur) < 3:
                    continue

            # CORRECTION : pour numero_facture, rejeter les valeurs qui ressemblent
            # à une date pure (ex: "2024" issu d'une date "15/03/2024")
            if champ == "numero_facture":
                vlow = valeur.lower()
                if vlow in {"renseignez", "saisissez", "facture", "numero", "n", "n°", "no"}:
                    continue
                # Un numéro de facture fiable contient presque toujours au moins un chiffre.
                if not re.search(r'\d', valeur):
                    continue
                # Rejeter si c'est uniquement une année sur 4 chiffres
                if re.fullmatch(r'\d{4}', valeur):
                    continue
                # Rejeter si la valeur est inférieure à 4 chars et purement numérique
                # (trop ambiguë — pourrait être une page, un mois, etc.)
                if len(valeur) < 3:
                    continue

            # Nettoyage des montants : supprimer unité DT/TND en fin
            if champ in {"montant_ht", "tva", "montant_ttc", "montant"}:
                valeur = _nettoyer_montant(valeur)

            if valeur:
                return valeur

    return None


def extraire_champs_entete(texte: str) -> dict:
    """
    Fonction principale — appelée par ocr_pipeline.py.

    Analyse le texte OCR, détecte le type de document,
    puis tente d'extraire chaque champ pertinent.
    Les champs non trouvés ont la valeur None (formulaire laissé vide).

    Args:
        texte: texte brut extrait par OCR

    Returns:
        {
            "type_document"   : str,
            "champs"          : { champ: valeur | None },
            "champs_trouves"  : [str, ...],
            "champs_manquants": [str, ...]
        }
    """
    type_doc = detecter_type_document(texte)
    champs_a_extraire = CHAMPS_PAR_TYPE.get(type_doc, CHAMPS_PAR_TYPE["inconnu"])

    tous_champs    = {}
    champs_trouves = []
    champs_manquants = []

    for champ in champs_a_extraire:
        valeur = extraire_champ(texte, champ)
        tous_champs[champ] = valeur
        if valeur is not None:
            champs_trouves.append(champ)
        else:
            champs_manquants.append(champ)

    return {
        "type_document":    type_doc,
        "champs":           tous_champs,
        "champs_trouves":   champs_trouves,
        "champs_manquants": champs_manquants,
    }