"""
extractor.py — Groupe Bayoudh Metal
Extracteur Intelligent v2

CORRECTIONS v2 :
  1. Patterns de détection plus tolérants (texte OCR tronqué/bruité)
  2. Regex fournisseur limité à 40 chars (évite pollution \nDate)
  3. Regex date accepte 3 chiffres pour l'année (OCR tronqué : 202x → 202)
  4. Détection numéro facture élargie (uméro, umero, n°, num, #)
  5. Montant TTC détecté même sans mot-clé "TTC" (heuristique = montant le plus grand)
  6. Nettoyage du fournisseur (suppression suffixes parasites)
"""

import re


# ─────────────────────────────────────────────────────────────────────
# NETTOYAGE TEXTE
# ─────────────────────────────────────────────────────────────────────

def clean_and_structure_text(raw_text):
    if not raw_text:
        return ""
    text = str(raw_text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────
# EXTRACTEUR INTELLIGENT v2
# ─────────────────────────────────────────────────────────────────────

class ExtracteurIntelligent:

    def __init__(self, texte_brut):
        self.texte       = texte_brut or ""
        self.texte_lower = self.texte.lower()

    # ─────────────────────────────────────────────────────────────
    # DÉTECTION TYPE DOCUMENT
    # ─────────────────────────────────────────────────────────────

    def detecter_type_document(self):
        scores = {"facture": 0, "bon_livraison": 0, "cheque": 0, "bon_commande": 0}

        mots = {
            # FIX 1 : ajout de variantes tronquées par l'OCR
            "facture": [
                "facture", "invoice", "fact.", "فاتورة",
                "montant ttc", "tva", "net à payer",
                "acture",           # "f" tronqué
                "factue",           # OCR manque un caractère
                "fac-",             # préfixe numéro facture
                "numero facture", "numéro facture",
                "umero facture",    # "n" tronqué
                "uméro facture",    # "n" tronqué avec accent
            ],
            "bon_livraison": [
                "bon de livraison", "livraison", "b.l", "bl n°", "bordereau",
                "bon livraison",
            ],
            "cheque": [
                "chèque", "cheque", "à l'ordre", "payer contre", "شيك", "rib",
                "chque",            # OCR manque accent
            ],
            "bon_commande": [
                "bon de commande", "commande", "purchase order", "b.c",
                "bon commande",
            ],
        }

        for type_doc, liste_mots in mots.items():
            for mot in liste_mots:
                if mot in self.texte_lower:
                    scores[type_doc] += 1

        type_detecte = max(scores, key=scores.get)
        return type_detecte if scores[type_detecte] > 0 else "inconnu"

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION DATES
    # ─────────────────────────────────────────────────────────────

    def extraire_dates(self):
        patterns = [
            r'\b(\d{2}[/-]\d{2}[/-]\d{4})\b',         # JJ/MM/AAAA
            r'\b(\d{4}[/-]\d{2}[/-]\d{2})\b',         # AAAA-MM-JJ
            r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b',   # JJ/MM/AA ou JJ/MM/AAAA
            # FIX 3 : tolérance 3 chiffres pour l'année (OCR tronqué)
            r'\b(\d{2}[/-]\d{2}[/-]\d{3})\b',          # JJ/MM/202 (tronqué)
        ]

        dates = []
        vus_valeurs = set()

        for p in patterns:
            for m in re.finditer(p, self.texte):
                val = m.group(1)
                if val in vus_valeurs:
                    continue
                vus_valeurs.add(val)
                contexte = self._contexte(m.start())
                dates.append({"valeur": val, "role": self._role_date(contexte)})

        vus_roles = {}
        for d in dates:
            if d["role"] not in vus_roles:
                vus_roles[d["role"]] = d["valeur"]

        return vus_roles

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION MONTANTS
    # ─────────────────────────────────────────────────────────────

    def extraire_montants(self):
        patterns = [
            r'(\d{1,3}(?:[\s.]\d{3})*(?:[.,]\d{2}))\s*(?:TND|DT|EUR|€|\$|دينار)?',
            # FIX 5 : capturer aussi les montants entiers (ex: "4 250" sans décimales)
            r'(\d{1,3}(?:\s\d{3})+)(?:\s*(?:TND|DT|EUR|€))?',
        ]

        # Contextes à exclure : numéros qui ne sont pas des montants
        CONTEXTES_EXCLUS = [
            "mf", "matricule", "rib", "iban", "bic", "swift",
            "tel", "tél", "fax", "rc", "registre", "code postal",
            "bp ", "b.p", "cp ", "siret", "siren", "ice"
        ]

        montants = []
        vus_valeurs = set()

        for p in patterns:
            for m in re.finditer(p, self.texte, re.IGNORECASE):
                val_brute = m.group(1)
                val = self._nettoyer_montant(val_brute)
                if val <= 0 or val_brute in vus_valeurs:
                    continue

                # Filtrer les montants aberrants (> 999 999 suspects pour une facture)
                if val > 999_999:
                    continue

                # Filtrer les valeurs dans un contexte non-montant
                contexte = self._contexte(m.start())
                if any(exc in contexte for exc in CONTEXTES_EXCLUS):
                    continue

                vus_valeurs.add(val_brute)
                montants.append({"valeur": val, "role": self._role_montant(contexte)})

        # Trier par valeur décroissante
        montants.sort(key=lambda x: x["valeur"], reverse=True)

        vus_roles = {}
        for mt in montants:
            if mt["role"] not in vus_roles:
                vus_roles[mt["role"]] = mt["valeur"]

        # FIX 5 : heuristique — si montant_ttc absent, le montant le plus élevé = TTC
        if "montant_ttc" not in vus_roles and montants:
            vus_roles["montant_ttc"] = montants[0]["valeur"]

        # Estimer montant_ht depuis TTC uniquement si vraiment absent
        # ET si montant_tva est aussi absent (évite double estimation)
        if ("montant_ht" not in vus_roles
                and "montant_ttc" in vus_roles
                and "montant_tva" not in vus_roles):
            ttc = vus_roles["montant_ttc"]
            # TVA Tunisie standard : 19% (taux le plus courant pour métaux)
            vus_roles["montant_ht"]  = round(ttc / 1.19, 2)
            vus_roles["montant_tva"] = round(ttc - ttc / 1.19, 2)

        return vus_roles

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION RÉFÉRENCES
    # ─────────────────────────────────────────────────────────────

    def extraire_references(self):
        patterns = [
            r'\b([A-Z]{1,3}[-/]\d{4}[-/]\d+)\b',          # FAC-2024-00142
            r'\b(\d{4}[-/]\d{3,6})\b',                     # 2024-00142
            # FIX 4 : variantes tronquées par l'OCR pour "numéro"
            r'(?:num[eé]ro|n[°o]\.?|num\.?|r[eé]f\.?|#|uméro|umero|umeéro)\s*'
            r'(?:facture|fact\.?|bl|commande|ch[eè]que)?\s*[:\s]*([A-Z0-9/\-]{3,})',
        ]

        refs = []
        vus_valeurs = set()

        for p in patterns:
            for m in re.finditer(p, self.texte, re.IGNORECASE):
                val = m.group(1).strip()
                if not val or len(val) < 3 or val in vus_valeurs:
                    continue
                vus_valeurs.add(val)
                contexte = self._contexte(m.start())
                refs.append({"valeur": val, "role": self._role_reference(contexte)})

        vus_roles = {}
        for r in refs:
            if r["role"] not in vus_roles:
                vus_roles[r["role"]] = r["valeur"]

        return vus_roles

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION NOMS (FOURNISSEUR)
    # ─────────────────────────────────────────────────────────────

    def extraire_noms(self):
        patterns = [
            # FIX 2 : limité à 40 chars, sans capture de \n ni de mots-clés suivants
            r'(?:fournisseur|supplier|vendeur|vendor)\s*[:\s]+'
            r'([A-ZÀ-Ÿ][A-Za-zÀ-ÿ0-9\s&]{2,38}?)(?=\s*(?:\n|$|MF|RIB|Tél|Tel|Date|Adresse|N°|F:|$))',

            # Nom suivi d'une forme juridique connue (SARL, SA, etc.) — limité à 50 chars
            r'([A-Z][A-Za-zÀ-ÿ0-9\s&,.\-]{2,44}'
            r'(?:SARL|SA\b|EURL|GROUP|GROUPE|SNC|LLC|SAS|GIE))',
        ]

        noms = []
        vus_valeurs = set()

        for p in patterns:
            for m in re.finditer(p, self.texte, re.IGNORECASE):
                val = m.group(1).strip()
                # FIX 6 : nettoyer les suffixes parasites
                val = self._nettoyer_nom(val)
                if not val or len(val) < 3 or val in vus_valeurs:
                    continue
                vus_valeurs.add(val)
                contexte = self._contexte(m.start())
                noms.append({"valeur": val, "role": self._role_nom(contexte)})

        vus_roles = {}
        for n in noms:
            if n["role"] not in vus_roles:
                vus_roles[n["role"]] = n["valeur"]

        return vus_roles

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION PRINCIPALE
    # ─────────────────────────────────────────────────────────────

    def extraire_tout(self):
        type_doc = self.detecter_type_document()
        champs   = {}
        champs.update(self.extraire_dates())
        champs.update(self.extraire_montants())
        champs.update(self.extraire_references())
        champs.update(self.extraire_noms())
        return {"type_document": type_doc, "champs": champs}

    # ─────────────────────────────────────────────────────────────
    # UTILITAIRES
    # ─────────────────────────────────────────────────────────────

    def _contexte(self, pos, fenetre=60):
        debut = max(0, pos - fenetre)
        fin   = min(len(self.texte), pos + fenetre)
        return self.texte[debut:fin].lower()

    def _nettoyer_montant(self, val):
        try:
            return float(val.replace(" ", "").replace(",", "."))
        except Exception:
            return 0

    def _nettoyer_nom(self, val):
        """
        FIX 6 : supprime les suffixes parasites capturés par les regex.
        Ex: 'ACIER TUNISIE SAR\nDate' → 'ACIER TUNISIE SAR'
        """
        # Couper au premier retour à la ligne
        val = val.split("\n")[0].split("\r")[0]
        # Supprimer les mots parasites en fin de chaîne
        mots_parasites = [
            r'\s*Date\s*$', r'\s*Adresse\s*$', r'\s*Tel\s*$',
            r'\s*Tél\s*$',  r'\s*MF\s*$',      r'\s*RIB\s*$',
            r'\s*N°\s*$',   r'\s*:\s*$',
        ]
        for p in mots_parasites:
            val = re.sub(p, '', val, flags=re.IGNORECASE)
        return val.strip()

    def _role_date(self, ctx):
        if "livraison" in ctx:
            return "date_livraison"
        if "échéance" in ctx or "echeance" in ctx or "paiement" in ctx:
            return "date_echeance"
        if "commande" in ctx:
            return "date_commande"
        return "date"

    def _role_montant(self, ctx):
        if "ttc" in ctx or "net à payer" in ctx or "total" in ctx or "net a payer" in ctx:
            return "montant_ttc"
        if "tva" in ctx or "taxe" in ctx:
            return "montant_tva"
        if "ht" in ctx or "hors taxe" in ctx or "hors-taxe" in ctx:
            return "montant_ht"
        if "remise" in ctx:
            return "remise"
        return "montant"

    def _role_reference(self, ctx):
        # FIX 4 : élargir les mots-clés de détection
        if any(k in ctx for k in ["facture", "invoice", "fact", "acture", "factue"]):
            return "numero_facture"
        if any(k in ctx for k in ["livraison", "bl ", "b.l"]):
            return "numero_bl"
        if any(k in ctx for k in ["commande", "bc ", "b.c"]):
            return "numero_commande"
        if any(k in ctx for k in ["cheque", "chèque", "chque"]):
            return "numero_cheque"
        return "reference"

    def _role_nom(self, ctx):
        if any(k in ctx for k in ["fournisseur", "vendeur", "supplier", "vendor"]):
            return "fournisseur"
        if any(k in ctx for k in ["client", "destinataire", "acheteur"]):
            return "client"
        if any(k in ctx for k in ["banque", "bank", "bnq"]):
            return "banque"
        return "societe"