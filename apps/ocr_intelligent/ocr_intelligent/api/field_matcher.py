"""
field_matcher.py — Groupe Bayoudh Metal
Cœur du scénario de remplissage automatique.

CORRECTIONS APPLIQUÉES :
  1. extracted_fields → extracted_field  (aligné sur le doctype, 2 endroits)
  2. _score_compatibilite : normalisation des montants avant comparaison
  3. _score_compatibilite : tolérance sur les formats de dates tronquées
  4. Intégration rapidfuzz pour matching flou (noms, références)
  5. Retour champs_compatibles / champs_incompatibles pour le frontend

Algorithme :
  1. Cherche tous les OCR Documents portant le même nom de fichier
  2. Trie les candidats par score de confiance (meilleur en premier)
  3. Pour chaque candidat → itère champ par champ du formulaire
     - Si un champ est INCOMPATIBLE → sort immédiatement (break)
       et passe au candidat suivant
     - Si TOUS les champs sont compatibles → retourne ce candidat
  4. Si aucun candidat ne passe → retourne une erreur détaillée
"""

import frappe
import json
import re
from collections import Counter

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _RAPIDFUZZ = True
except ImportError:
    _RAPIDFUZZ = False


# ──────────────────────────────────────────────────────────────────────
# SEUILS DE COMPATIBILITÉ
# ──────────────────────────────────────────────────────────────────────

SEUIL_SCORE_MIN        = 0.35   # Similarité minimale pour considérer un champ comme compatible
SEUIL_CANDIDAT_VALIDE  = 0.50   # Ratio de champs compatibles pour valider un candidat
SEUIL_TEXTE_PRESENT    = 0.25   # Présence minimale du mot dans le texte OCR


# ──────────────────────────────────────────────────────────────────────
# MAPPING TYPE DOCUMENT → CHAMPS ATTENDUS
# Format : { "champ_ocr": "fieldname_frappe" }
# ──────────────────────────────────────────────────────────────────────

MAPPING_CHAMPS = {
    "facture": {
        "numero_facture":  "bill_no",
        "date":            "bill_date",
        "fournisseur":     "supplier",
        "montant_ht":      "net_total",
        "montant_tva":     "total_taxes_and_charges",
        "montant_ttc":     "grand_total",
        "date_echeance":   "due_date",
        "mode_paiement":   "payment_terms_template",
        "numero_commande": "po_no",
    },
    "bon_livraison": {
        "numero_bl":      "lr_no",
        "date_livraison": "lr_date",
        "fournisseur":    "supplier",
    },
    "cheque": {
        "numero_cheque":  "reference_no",
        "montant":        "paid_amount",
        "date_cheque":    "reference_date",
        "cheque_date":    "reference_date",
        "date":           "reference_date",
        "banque":         "bank",
        "beneficiaire":   "party",
    },
    "traite": {
        "montant":        "paid_amount",
        "amount":         "paid_amount",      # alias payment_doc_extractor
        "date_echeance":  "reference_date",
        "due_date":       "reference_date",   # alias normalisé
        "date_emission":  "reference_date",   # fallback si échéance absente
        "issue_date":     "reference_date",   # alias normalisé
        "tireur":         "party",
        "drawer":         "party",            # alias normalisé
        "tire":           "bank",
        "drawee":         "bank",             # alias normalisé
        "numero_traite":  "reference_no",
        "draft_number":   "reference_no",     # alias normalisé
    },
    "bon_commande": {
        "numero_commande": "po_no",
        "date_commande":   "transaction_date",
        "fournisseur":     "supplier",
        "montant_ttc":     "grand_total",
    },
    "inconnu": {
        "date":           "posting_date",
        "montant_ttc":    "grand_total",
        "fournisseur":    "supplier",
        "reference":      "reference_no",
        "numero_facture": "bill_no",
    }
}


# ──────────────────────────────────────────────────────────────────────
# FONCTION PRINCIPALE
# ──────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def trouver_meilleur_candidat(nom_fichier, champs_formulaire=None):
    """
    Cherche le meilleur OCR Document pour remplir le formulaire.

    Paramètres :
        nom_fichier      : nom du fichier uploadé (ex: "cheque.jpg")
        champs_formulaire: dict optionnel {fieldname: valeur_actuelle}
                           Si fourni, améliore le score de compatibilité

    Retourne :
    {
        "success"         : bool,
        "candidat_choisi" : "OCR-DOC-0017",
        "champs_remplis"  : {"bill_no": "FAC-001", ...},
        "type_document"   : "facture",
        "score_final"     : 0.82,
        "nb_candidats"    : 3,
        "iterations"      : [ détail de chaque candidat testé ],
        "erreur"          : "..." si success=False,
        "champ_bloquant"  : "montant_ttc" si incompatible
    }
    """
    if not nom_fichier:
        return {"success": False, "erreur": "Nom de fichier requis."}

    if isinstance(champs_formulaire, str):
        try:
            champs_formulaire = json.loads(champs_formulaire)
        except Exception:
            champs_formulaire = {}

    champs_formulaire = champs_formulaire or {}

    # ── 1. Récupérer tous les candidats ──────────────────────────────
    candidats = _charger_candidats(nom_fichier)

    if not candidats:
        return {
            "success": False,
            "erreur": (
                f"Aucun OCR Document trouvé pour '{nom_fichier}'.\n"
                "Veuillez d'abord téléverser le fichier via le pipeline OCR."
            ),
            "nb_candidats": 0
        }

    # ── 2. Boucle d'itération sur les candidats ──────────────────────
    journal_iterations = []
    meilleur_resultat  = None
    meilleur_score     = -1

    for idx, candidat in enumerate(candidats):

        iteration_info = {
            "index":          idx,
            "ocr_doc_id":     candidat["name"],
            "score_ocr":      candidat.get("confidence_score", 0),
            "champs_testes":  [],
            "resultat":       None,
            "champ_bloquant": None,
        }

        champs_extraits = candidat.get("_champs_parsed", {})
        type_doc        = candidat.get("_type_doc", "inconnu")
        texte_brut      = candidat.get("extracted_text", "")
        mapping         = MAPPING_CHAMPS.get(type_doc, MAPPING_CHAMPS["inconnu"])

        nb_compatibles  = 0
        nb_total        = 0
        champ_bloquant  = None

        # ── Boucle champ par champ ────────────────────────────────────
        for champ_ocr, fieldname_frappe in mapping.items():

            nb_total += 1
            valeur_ocr = champs_extraits.get(champ_ocr)

            # Si la valeur OCR est absente → champ neutre (ne bloque pas)
            if not valeur_ocr:
                iteration_info["champs_testes"].append({
                    "champ_ocr":  champ_ocr,
                    "fieldname":  fieldname_frappe,
                    "valeur_ocr": None,
                    "compatible": None,
                    "raison":     "absent dans OCR"
                })
                continue

            valeur_str   = str(valeur_ocr).strip()
            score_compat = _score_compatibilite(
                champ_ocr, valeur_str, texte_brut, champs_formulaire.get(fieldname_frappe)
            )
            est_compatible = score_compat >= SEUIL_SCORE_MIN

            iteration_info["champs_testes"].append({
                "champ_ocr":   champ_ocr,
                "fieldname":   fieldname_frappe,
                "valeur_ocr":  valeur_str,
                "valeur_form": champs_formulaire.get(fieldname_frappe),
                "score":       round(score_compat, 3),
                "compatible":  est_compatible,
            })

            if est_compatible:
                nb_compatibles += 1
            else:
                # ── SORTIE IMMÉDIATE : champ incompatible ─────────────
                champ_bloquant                   = champ_ocr
                iteration_info["champ_bloquant"] = champ_ocr
                iteration_info["resultat"]       = "rejeté"
                break  # ← sortie de la boucle interne

        else:
            # Tous les champs ont été parcourus sans break
            ratio = nb_compatibles / nb_total if nb_total > 0 else 0
            iteration_info["ratio_compatibilite"] = round(ratio, 3)
            iteration_info["resultat"] = (
                "validé" if ratio >= SEUIL_CANDIDAT_VALIDE else "ratio insuffisant"
            )

            if ratio >= SEUIL_CANDIDAT_VALIDE and ratio > meilleur_score:
                meilleur_score    = ratio
                meilleur_resultat = {
                    "candidat":      candidat,
                    "type_doc":      type_doc,
                    "champs_ocr":    champs_extraits,
                    "mapping":       mapping,
                    "ratio":         ratio,
                    "iteration_idx": idx,
                }

        journal_iterations.append(iteration_info)

    # ── 3. Résultat final ─────────────────────────────────────────────
    if meilleur_resultat:
        champs_remplis = _construire_champs_remplis(
            meilleur_resultat["champs_ocr"],
            meilleur_resultat["mapping"]
        )
        return {
            "success":         True,
            "candidat_choisi": meilleur_resultat["candidat"]["name"],
            "type_document":   meilleur_resultat["type_doc"],
            "champs_remplis":  champs_remplis,
            "score_final":     round(meilleur_resultat["ratio"], 3),
            "nb_candidats":    len(candidats),
            "iterations":      journal_iterations,
            "message": (
                f"Document identifié : {meilleur_resultat['candidat']['name']} "
                f"({len(champs_remplis)} champ(s) rempli(s), "
                f"compatibilité {round(meilleur_resultat['ratio']*100)}%)"
            )
        }

    else:
        raison = _construire_message_echec(journal_iterations, nom_fichier)
        return {
            "success":        False,
            "erreur":         raison["message"],
            "champ_bloquant": raison["champ_bloquant"],
            "nb_candidats":   len(candidats),
            "iterations":     journal_iterations,
            "conseil":        raison["conseil"],
        }


# ──────────────────────────────────────────────────────────────────────
# CHARGEMENT DES CANDIDATS
# ──────────────────────────────────────────────────────────────────────

def _charger_candidats(nom_fichier):
    """
    Retourne la liste des OCR Documents pour ce nom de fichier,
    triés par score de confiance décroissant (meilleur candidat en premier).
    """
    try:
        docs = frappe.get_list(
            "OCR Document",
            filters={"document_name": nom_fichier},
            fields=[
                "name", "document_name",
                "extracted_field",
                "extracted_text", "confidence_score", "status"
            ],
            order_by="confidence_score desc",
            limit=20
        )
    except Exception as e:
        frappe.log_error(str(e), "FieldMatcher - chargement candidats")
        return []

    candidats_enrichis = []
    for doc in docs:
        champs_parsed = {}

        try:
            raw = doc.get("extracted_field") or "{}"
            champs_parsed = json.loads(raw)
        except Exception:
            champs_parsed = {}

        doc["_champs_parsed"] = champs_parsed
        doc["_type_doc"]      = _detecter_type(champs_parsed)
        candidats_enrichis.append(doc)

    return candidats_enrichis


def _detecter_type(champs):
    """Détecte le type de document depuis les clés présentes dans les champs extraits."""
    cles = set(k.lower() for k in champs.keys())
    if any(c in cles for c in ["numero_facture", "montant_ttc", "montant_tva"]):
        return "facture"
    if any(c in cles for c in ["numero_bl", "date_livraison"]):
        return "bon_livraison"
    if any(c in cles for c in ["numero_traite", "tireur", "tire"]):
        return "traite"
    if any(c in cles for c in ["numero_cheque", "banque"]):
        return "cheque"
    if any(c in cles for c in ["numero_commande", "date_commande"]):
        return "bon_commande"
    return "inconnu"


# ──────────────────────────────────────────────────────────────────────
# SCORE DE COMPATIBILITE
# ──────────────────────────────────────────────────────────────────────

def _normaliser_montant(val):
    """
    Normalise un montant pour la comparaison :
    '5 950,00' → '5950.0'  |  '5950.0' → '5950.0'
    """
    try:
        normalise = val.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        return str(float(normalise))
    except Exception:
        return val.replace(" ", "").replace(",", ".")


def _normaliser_date(val):
    """
    Normalise une date tronquée pour comparaison souple :
    '15/04/202' → '15/04/202'  (on garde, et on compare aussi les 8 premiers chars)
    """
    return re.sub(r'[\s]', '', val)


def _score_compatibilite(champ_ocr, valeur_ocr, texte_brut, valeur_formulaire=None):
    """
    Calcule un score de compatibilité entre la valeur OCR et le contexte.
    Score de 0.0 à 1.0.

    Critères (cumulatifs) :
      1. Présence dans le texte brut          (0.4 pts)
      2. Cohérence de format selon le champ   (0.3 pts)
      3. Correspondance avec valeur formulaire (0.3 pts)
    """
    score = 0.0
    val   = str(valeur_ocr).strip()
    texte = texte_brut or ""

    # ── Critère 1 : Présence dans le texte brut ───────────────────────
    val_lower   = val.lower()
    texte_lower = texte.lower()

    if val_lower in texte_lower:
        score += 0.4
    else:
        # ── Normalisation des montants ───────────────────────────────
        est_montant = any(k in champ_ocr.lower() for k in ["montant", "total", "ttc", "ht", "tva"])
        est_date    = "date" in champ_ocr.lower()

        if est_montant:
            val_norm   = _normaliser_montant(val)
            texte_norm = re.sub(r'[\s\u00a0]', '', texte_lower).replace(",", ".")
            # Chercher la valeur normalisée OU les chiffres sans décimales
            val_entier = val_norm.split(".")[0]
            if val_norm in texte_norm or val_entier in texte_norm:
                score += 0.4
            else:
                # Comparaison partielle sur les chiffres significatifs
                chiffres_val = re.sub(r'[^\d]', '', val)
                chiffres_txt = re.sub(r'[^\d]', '', texte)
                if len(chiffres_val) >= 3 and chiffres_val in chiffres_txt:
                    score += 0.3

        elif est_date:
            # ── Tolerance sur dates tronquees ────────────────────────
            val_norm   = _normaliser_date(val)
            texte_norm = re.sub(r'[\s]', '', texte_lower)
            # Comparer les 8 premiers caractères (JJ/MM/AAAA → JJ/MM/AA)
            if val_norm[:8] in texte_norm or val_norm in texte_norm:
                score += 0.4
            else:
                # Chercher les composantes de la date individuellement
                parties = re.split(r'[/\-\.]', val_norm)
                if parties:
                    presents = sum(1 for p in parties if p and p in texte_lower)
                    score += 0.4 * (presents / len(parties))

        else:
            # Vérification mot par mot + rapidfuzz pour les autres champs
            mots = [m for m in re.split(r'[\s\-/]+', val_lower) if len(m) > 1]
            if mots:
                presents = sum(1 for m in mots if m in texte_lower)
                score += 0.4 * (presents / len(mots))
            # Bonus rapidfuzz si présence partielle faible
            if _RAPIDFUZZ and score < 0.2 and len(val_lower) > 3:
                ratio = _rf_fuzz.partial_ratio(val_lower, texte_lower) / 100.0
                score += 0.4 * min(ratio, 0.9)

    # ── Critère 2 : Cohérence de format ──────────────────────────────
    score += _score_format(champ_ocr, valeur_ocr)

    # ── Critère 3 : Correspondance formulaire ─────────────────────────
    if valeur_formulaire is not None:
        val_form = str(valeur_formulaire).strip().lower()
        val_ocr  = val.lower()

        if val_form == val_ocr:
            score += 0.3
        elif val_form in val_ocr or val_ocr in val_form:
            score += 0.15
        elif _RAPIDFUZZ:
            # Matching flou avec rapidfuzz pour fournisseurs, références
            est_montant = any(k in champ_ocr.lower() for k in ["montant", "total", "ttc", "ht"])
            if est_montant:
                try:
                    if abs(float(_normaliser_montant(val_form)) -
                           float(_normaliser_montant(val_ocr))) < 0.01:
                        score += 0.3
                except Exception:
                    pass
            else:
                ratio = _rf_fuzz.token_set_ratio(val_form, val_ocr) / 100.0
                if ratio >= 0.80:
                    score += 0.3
                elif ratio >= 0.55:
                    score += 0.15
        else:
            # Comparaison normalisée pour les montants (sans rapidfuzz)
            est_montant = any(k in champ_ocr.lower() for k in ["montant", "total", "ttc", "ht"])
            if est_montant:
                try:
                    if abs(float(_normaliser_montant(val_form)) -
                           float(_normaliser_montant(val_ocr))) < 0.01:
                        score += 0.3
                except Exception:
                    pass

    return min(score, 1.0)


def _score_format(champ_ocr, valeur):
    """Vérifie que le format de la valeur correspond au type de champ attendu."""
    val = str(valeur).strip()

    formats = {
        # Dates : JJ/MM/AAAA ou AAAA-MM-JJ (tolérance 3 chiffres pour l'année)
        "date":            r'\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{2,4}',
        "date_livraison":  r'\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{2,4}',
        "date_echeance":   r'\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{2,4}',
        "date_commande":   r'\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{2,4}',
        # Montants : nombre avec virgule/point
        "montant_ttc":     r'\d[\d\s]*[,\.]?\d*',
        "montant_ht":      r'\d[\d\s]*[,\.]?\d*',
        "montant_tva":     r'\d[\d\s]*[,\.]?\d*',
        "montant":         r'\d[\d\s]*[,\.]?\d*',
        # Références : alphanumériques avec tirets
        "numero_facture":  r'[A-Za-z0-9\-/]{3,}',
        "numero_bl":       r'[A-Za-z0-9\-/]{3,}',
        "numero_cheque":   r'\d{5,}',
        "numero_commande": r'[A-Za-z0-9\-/]{3,}',
        # Noms : lettres et espaces (limité à 40 chars pour éviter pollution)
        "fournisseur":     r'[A-Za-zÀ-ÿ\s]{3,40}',
        "client":          r'[A-Za-zÀ-ÿ\s]{3,40}',
        "banque":          r'[A-Za-zÀ-ÿ\s]{3,40}',
    }

    pattern = formats.get(champ_ocr.lower())
    if not pattern:
        return 0.3  # Pas de contrainte connue → neutre

    return 0.3 if re.search(pattern, val, re.IGNORECASE) else 0.0


# ──────────────────────────────────────────────────────────────────────
# CONSTRUCTION DES CHAMPS À REMPLIR
# ──────────────────────────────────────────────────────────────────────

def _construire_champs_remplis(champs_ocr, mapping):
    """
    Construit le dict final {fieldname_frappe: valeur}
    à partir des champs OCR et du mapping.
    """
    champs_remplis = {}
    for champ_ocr, fieldname_frappe in mapping.items():
        valeur = champs_ocr.get(champ_ocr)
        if valeur is not None and str(valeur).strip():
            champs_remplis[fieldname_frappe] = valeur
    return champs_remplis


# ──────────────────────────────────────────────────────────────────────
# MESSAGE D'ERREUR PRÉCIS
# ──────────────────────────────────────────────────────────────────────

def _construire_message_echec(iterations, nom_fichier):
    """
    Construit un message d'erreur précis basé sur le journal des itérations.
    Identifie le champ bloquant le plus fréquent.
    """
    champs_bloquants = [
        it["champ_bloquant"]
        for it in iterations
        if it.get("champ_bloquant")
    ]

    candidats_rejetes = len([it for it in iterations if it.get("resultat") == "rejeté"])
    candidats_ratio   = len([it for it in iterations if it.get("resultat") == "ratio insuffisant"])
    total             = len(iterations)

    if champs_bloquants:
        champ_plus_frequent = Counter(champs_bloquants).most_common(1)[0][0]
        message = (
            f"Aucun des {total} document(s) '{nom_fichier}' ne correspond au formulaire.\n"
            f"Champ incompatible le plus fréquent : '{champ_plus_frequent}'\n"
            f"({candidats_rejetes} candidat(s) rejeté(s) sur incompatibilité de champ)"
        )
        conseil = (
            f"Vérifiez que la valeur du champ '{champ_plus_frequent}' dans votre document "
            f"correspond bien aux données attendues dans le formulaire."
        )
        return {
            "message":        message,
            "champ_bloquant": champ_plus_frequent,
            "conseil":        conseil,
        }

    elif candidats_ratio > 0:
        message = (
            f"Les {total} document(s) '{nom_fichier}' ont été testés mais "
            f"le taux de correspondance est insuffisant ({candidats_ratio} candidat(s)).\n"
            "Le document ne contient pas assez de champs identifiables."
        )
        return {
            "message":  message,
            "champ_bloquant": None,
            "conseil":  "Vérifiez la qualité du scan ou utilisez un PDF natif.",
        }

    else:
        return {
            "message":        f"Aucun document OCR trouvé pour '{nom_fichier}'.",
            "champ_bloquant": None,
            "conseil":        "Téléversez d'abord le document via le bouton OCR.",
        }