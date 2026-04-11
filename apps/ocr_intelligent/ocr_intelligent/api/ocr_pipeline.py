"""
ocr_pipeline.py - Groupe Bayoudh Metal

Scénario :
  1. Bouton "Analyser document (OCR)" dans la Facture d'Achat
  2. Upload du fichier → extraction OCR
  3. Analyse de l'EN-TÊTE du document pour identifier le type et les champs
  4. Sauvegarde dans OCR Document
  5. Retourne les champs remplis → fenêtre de révision → Facture d'Achat remplie
"""

import frappe
import os
import re
import json
import time
import random
import cv2
import numpy as np
import pytesseract
from PIL import Image as PILImage, ImageEnhance, ImageOps
from werkzeug.utils import secure_filename


# ──────────────────────────────────────────────────────────────────────
# Normalisation des noms de champs header_extractor → MAPPING_CHAMPS
# ──────────────────────────────────────────────────────────────────────

_NORMALISE_CHAMPS = {
    "echeance":  "date_echeance",
    "validite":  "date_echeance",
    "tva":       "montant_tva",
}


# ──────────────────────────────────────────────────────────────────────
# UTILITAIRE : Nettoyage du nom de fichier (hash Frappe)
# ──────────────────────────────────────────────────────────────────────

def _nettoyer_nom_frappe(nom):
    base, ext = os.path.splitext(nom)
    base_nettoye = re.sub(r'[_\-]?[a-f0-9]{6,10}$', '', base, flags=re.IGNORECASE)
    if base_nettoye and base_nettoye != base:
        return base_nettoye + ext
    return nom


def _variantes_nom(nom_fichier):
    variantes = [nom_fichier]
    nom_nettoye = _nettoyer_nom_frappe(nom_fichier)
    if nom_nettoye != nom_fichier:
        variantes.append(nom_nettoye)
    base = os.path.splitext(nom_fichier)[0]
    if base not in variantes:
        variantes.append(base)
    base_nettoye = os.path.splitext(nom_nettoye)[0]
    if base_nettoye not in variantes:
        variantes.append(base_nettoye)
    return variantes


# ──────────────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ──────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def pipeline_complet(file_url=""):
    """
    Accepte soit file_url (frappe.call) soit form-data (POST brut).
    """
    contenu      = None
    nom_original = None
    content_type = ""

    # ── Cas 1 : file_url passé via frappe.call ────────────────────────
    if file_url:
        site_path = frappe.get_site_path()
        if file_url.startswith("/private/"):
            chemin_fichier = os.path.join(site_path, "private", "files",
                                          os.path.basename(file_url))
        elif file_url.startswith("/files/"):
            chemin_fichier = os.path.join(site_path, "public", "files",
                                          os.path.basename(file_url))
        else:
            chemin_fichier = os.path.join(site_path, "public", file_url.lstrip("/"))

        if not os.path.exists(chemin_fichier):
            return {"success": False, "erreur": f"Fichier introuvable : {file_url}"}

        nom_original = os.path.basename(chemin_fichier)
        with open(chemin_fichier, "rb") as f:
            contenu = f.read()

    # ── Cas 2 : form-data ─────────────────────────────────────────────
    else:
        files = frappe.request.files
        if not files or "file" not in files:
            return {"success": False, "erreur": "Aucun fichier reçu (form-data, clé: 'file')."}
        file_obj     = files["file"]
        nom_original = file_obj.filename
        content_type = getattr(file_obj, "content_type", "") or ""
        contenu      = file_obj.read()

    nom_original = secure_filename(nom_original)

    # ── Validation taille minimale ────────────────────────────────────
    taille_kb = len(contenu) / 1024
    if taille_kb < 2:
        return {
            "success": False,
            "erreur": (
                f"Fichier trop petit ({taille_kb:.1f} KB). "
                "Une facture/document lisible fait généralement entre 50 KB et 5 MB. "
                "Vérifiez que le fichier contient bien une image et non une icône."
            )
        }

    nom_fichier, ext = _corriger_extension(nom_original, content_type, contenu)
    extensions_ok    = [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]
    if ext not in extensions_ok:
        return {
            "success": False,
            "erreur": f"Format '{ext}' non supporté. Acceptés : {', '.join(extensions_ok)}"
        }

    dossier_tmp = os.path.join(frappe.get_site_path(), "private", "files")
    os.makedirs(dossier_tmp, exist_ok=True)
    chemin_tmp  = os.path.join(
        dossier_tmp,
        f"ocr_tmp_{int(time.time())}_{random.randint(0, 9999)}_{nom_fichier}"
    )

    try:
        with open(chemin_tmp, "wb") as f:
            f.write(contenu)

        # ── OCR ───────────────────────────────────────────────────────
        from ocr_intelligent.ocr.ocr_engine import OCREngine
        engine = OCREngine()
        try:
            res_ocr = engine.extraire_texte(chemin_tmp)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "OCR Pipeline")
            return {"success": False, "erreur": f"Erreur OCR : {str(e)}"}

        texte_brut = res_ocr.get("texte", "")
        score      = res_ocr.get("score_confiance", 0)
        nb_pages   = res_ocr.get("nombre_pages", 1)
        methode    = res_ocr.get("methode", "—")
        psm        = res_ocr.get("psm_selectionne", "—")
        debug_info = res_ocr.get("debug", {})

        # ── Validation texte extrait ──────────────────────────────────
        mots = [m for m in (texte_brut or "").split() if len(m) > 1]

        if len(mots) < 1:
            return {
                "success":         False,
                "score_confiance": score,
                "texte_extrait":   texte_brut[:500] if texte_brut else "",
                "erreur": (
                    f"Aucun texte détecté (score OCR {score}%, 0 mot). "
                    "Utilisez une image de bonne qualité (min. 50 KB, 300 DPI)."
                )
            }

        # ── Extraction depuis l'EN-TÊTE du document ───────────────────
        from ocr_intelligent.ocr.header_extractor import extraire_champs_entete
        from ocr_intelligent.ocr.nlp_analyzer     import analyser_contexte
        from ocr_intelligent.api.field_matcher    import MAPPING_CHAMPS

        lignes       = texte_brut.splitlines()
        texte_entete = "\n".join(lignes[:min(30, len(lignes))])

        if len(texte_entete.split()) < 3:
            texte_entete = texte_brut

        resultat_entete = extraire_champs_entete(texte_entete)
        type_doc        = resultat_entete["type_document"]

        # Normaliser les noms de champs (echeance→date_echeance, tva→montant_tva)
        champs_valides = {}
        for champ, valeur in resultat_entete["champs"].items():
            if valeur is not None and str(valeur).strip():
                champ_norm = _NORMALISE_CHAMPS.get(champ, champ)
                champs_valides[champ_norm] = valeur

        # ── Passe OCR dédiée aux MONTANTS (2ème passe sur zone tableau) ──
        montants_image = _extraire_montants_image(chemin_tmp)
        for champ_m, val_m in montants_image.items():
            if champ_m not in champs_valides and val_m:
                champs_valides[champ_m] = str(round(val_m, 3))

        # Si l'en-tête n'a pas donné assez de champs, relancer sur le texte complet
        if len(champs_valides) < 2 and texte_entete != texte_brut:
            resultat_complet = extraire_champs_entete(texte_brut)
            if resultat_complet["type_document"] != "inconnu":
                type_doc = resultat_complet["type_document"]
            for champ, valeur in resultat_complet["champs"].items():
                champ_norm = _NORMALISE_CHAMPS.get(champ, champ)
                if valeur and champ_norm not in champs_valides:
                    champs_valides[champ_norm] = valeur

        # ── Analyse NLP : enrichissement + détection contexte ─────────
        # Récupérer les champs du formulaire passés en paramètre (optionnel)
        champs_formulaire_param = frappe.form_dict.get("champs_formulaire") or {}
        if isinstance(champs_formulaire_param, str):
            try:
                champs_formulaire_param = json.loads(champs_formulaire_param)
            except Exception:
                champs_formulaire_param = {}

        analyse_nlp = analyser_contexte(
            texte           = texte_brut,
            champs_regex    = champs_valides,
            champs_formulaire = champs_formulaire_param,
            type_doc_force  = type_doc if type_doc != "inconnu" else None,
        )

        # Fusionner les champs enrichis par NLP dans champs_valides
        for champ, meta in analyse_nlp["champs_enrichis"].items():
            if champ not in champs_valides and meta.get("confiance", 0) >= 0.4:
                champs_valides[champ] = meta["valeur"]

        # Utiliser le type NLP si regex a échoué
        if type_doc == "inconnu" and analyse_nlp["type_document"] != "inconnu":
            type_doc = analyse_nlp["type_document"]

        # ── Mapping vers les champs Frappe (Facture d'Achat) ──────────
        mapping        = MAPPING_CHAMPS.get(type_doc, MAPPING_CHAMPS["inconnu"])
        champs_remplis = {}
        for champ_ocr, fieldname_frappe in mapping.items():
            valeur = champs_valides.get(champ_ocr)
            if valeur:
                champs_remplis[fieldname_frappe] = valeur

        # Dupliquer bill_date → posting_date si absent
        if "bill_date" in champs_remplis and "posting_date" not in champs_remplis:
            champs_remplis["posting_date"] = champs_remplis["bill_date"]

        # ── Statut OCR Document ────────────────────────────────────────
        statut = "Validé" if len(champs_remplis) >= 3 else "Validation requise"

        # ── Sauvegarde OCR Document ────────────────────────────────────
        ocr_doc_name = None
        for variante in _variantes_nom(nom_fichier):
            existants = frappe.get_list(
                "OCR Document",
                filters={"document_name": variante},
                fields=["name"],
                limit=1
            )
            if existants:
                ocr_doc_name = existants[0]["name"]
                break

        if ocr_doc_name:
            frappe.db.set_value("OCR Document", ocr_doc_name, {
                "extracted_text":   texte_brut,
                "extracted_field":  json.dumps(champs_valides, ensure_ascii=False, indent=2),
                "confidence_score": score,
                "status":           statut,
            })
        else:
            ocr_doc = frappe.get_doc({
                "doctype":          "OCR Document",
                "document_name":    nom_fichier,
                "uploaded_by":      frappe.session.user,
                "confidence_score": score,
                "extracted_text":   texte_brut,
                "extracted_field":  json.dumps(champs_valides, ensure_ascii=False, indent=2),
                "status":           statut,
            })
            ocr_doc.insert(ignore_permissions=True)
            ocr_doc_name = ocr_doc.name

        frappe.db.commit()

        # ── Résultat ──────────────────────────────────────────────────
        if not champs_remplis:
            return {
                "success":              False,
                "nom_fichier":          nom_fichier,
                "ocr_document_id":      ocr_doc_name,
                "score_confiance":      score,
                "type_document":        type_doc,
                "texte_extrait":        texte_brut[:300],
                "champs_compatibles":   analyse_nlp.get("champs_compatibles", []),
                "champs_incompatibles": analyse_nlp.get("champs_incompatibles", []),
                "suggestion_nlp":       analyse_nlp.get("suggestion", ""),
                "erreur": (
                    f"Document analysé (type détecté : {type_doc}) "
                    "mais aucun champ n'a pu être extrait depuis l'en-tête. "
                    "Vérifiez la qualité du document."
                ),
                "conseil": "Préférez un PDF natif ou une image nette ≥ 300 DPI.",
            }

        return {
            "success":              True,
            "nom_fichier":          nom_fichier,
            "type_document":        type_doc,
            "champs_remplis":       champs_remplis,
            "score_confiance":      score,
            "nombre_pages":         nb_pages,
            "methode_ocr":          methode,
            "ocr_document_id":      ocr_doc_name,
            "texte_extrait":        texte_brut[:500],
            # ── Résultats NLP ─────────────────────────────────────────
            "score_type_document":  analyse_nlp.get("score_type", 0),
            "champs_enrichis":      {
                champ: meta["valeur"]
                for champ, meta in analyse_nlp.get("champs_enrichis", {}).items()
            },
            "champs_compatibles":   analyse_nlp.get("champs_compatibles", []),
            "champs_incompatibles": analyse_nlp.get("champs_incompatibles", []),
            "entites_nlp":          analyse_nlp.get("entites", {}),
            "suggestion_nlp":       analyse_nlp.get("suggestion", ""),
            "message": (
                f"{len(champs_remplis)} champ(s) rempli(s) depuis l'en-tête "
                f"(type : {type_doc}, score OCR : {score}%, "
                f"NLP : {analyse_nlp.get('nb_compatibles', 0)} compatible(s), "
                f"{analyse_nlp.get('nb_incompatibles', 0)} incompatible(s))"
            ),
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Pipeline Error")
        return {"success": False, "erreur": f"Erreur inattendue : {str(e)}"}

    finally:
        if os.path.exists(chemin_tmp):
            try:
                os.remove(chemin_tmp)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────
# PASSE OCR DÉDIÉE AUX MONTANTS (chiffres dans le tableau)
# ──────────────────────────────────────────────────────────────────────

def _extraire_montants_image(chemin_fichier):
    """
    Deuxième passe OCR ciblée sur les montants (zone totaux).
    Utilise un prétraitement adapté aux factures tunisiennes :
    - Seuillage adaptatif + suppression grille
    - PSM 11 (sparse) + PSM 4 (column)
    - Format tunisien : virgule = séparateur décimal, espace/point = milliers
    """
    try:
        ext = os.path.splitext(chemin_fichier)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_fichier, dpi=300)
            if not pages:
                return {}
            img_cv = cv2.cvtColor(np.array(pages[0]), cv2.COLOR_RGB2BGR)
        else:
            img_cv = cv2.imread(chemin_fichier)
            if img_cv is None:
                pil = PILImage.open(chemin_fichier).convert("RGB")
                img_cv = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

        h, w = img_cv.shape[:2]

        # Zone des totaux : bas 45 % de l'image
        zone = img_cv[int(h * 0.55):, :]
        gris = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY) if len(zone.shape) == 3 else zone

        # Agrandir pour meilleure OCR
        hh, ww = gris.shape[:2]
        if ww < 2500:
            scale = 2500 / ww
            gris = cv2.resize(gris, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        # Seuillage adaptatif (meilleur que Otsu pour les tableaux)
        binaire = cv2.adaptiveThreshold(
            gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
        )

        # Supprimer les lignes de grille (horizontales + verticales)
        horizontal = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((1, 40), np.uint8))
        vertical = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((40, 1), np.uint8))
        grille = cv2.add(horizontal, vertical)
        propre = cv2.add(binaire, grille)

        pil_img = PILImage.fromarray(propre)

        # Passe 1 : PSM 11 (sparse text) – lit chaque bloc de texte séparément
        texte_sparse = pytesseract.image_to_string(
            pil_img, lang="fra+eng", config="--oem 3 --psm 11"
        )

        # Passe 2 : PSM 4 (colonnes) – conserve les labels avec les nombres
        texte_col = pytesseract.image_to_string(
            pil_img, lang="fra+eng", config="--oem 3 --psm 4"
        )

        # Essayer le parsing sur chaque texte et prendre le meilleur
        r_sparse = _parser_montants(texte_sparse)
        r_col = _parser_montants(texte_col)

        # Merge intelligent : pour chaque champ, choisir la valeur la plus cohérente
        resultats = _fusionner_montants(r_sparse, r_col)

        return resultats

    except Exception as e:
        try:
            frappe.log_error(f"Extraction montants image: {e}", "OCR Montants")
        except Exception:
            pass
        return {}


def _parser_montants(texte):
    """
    Parse le texte OCR de la zone totaux.
    Gère le cas PSM 11 où labels et nombres sont sur des lignes séparées.
    """
    resultats = {}
    lignes = texte.splitlines()

    # Patterns pour identifier les labels (ordre important: plus spécifique d'abord)
    label_patterns_ordered = [
        ("_base_tva", r"(?:base\s*tva|baso\s*tva)"),
        ("montant_ttc", r"(?:t\.?t\.?c|net\s*[àa]\s*payer|total\s*g[ée]n|leite|amount\s*due)"),
        ("montant_ht", r"(?:total\s*h\.?t|hors\s*taxe|sous.?total|net\s*ht|montant\s*h\.?t|\btear\b)"),
        ("_fodec", r"(?:fodec|fodeg)"),
        ("_timbre", r"(?:timbre|timbr)"),
        ("montant_tva", r"(?:\btva\b|t\.v\.a|total\s*tax|tottex)"),
    ]

    # Plage de montants vraisemblables (en TND)
    MONTANT_MIN = 0.5
    MONTANT_MAX = 99999

    def _est_montant_valide(n):
        return n is not None and MONTANT_MIN < n < MONTANT_MAX

    def _est_ligne_footer(ligne):
        """Détecte les lignes de pied de page (adresse, tél, fax, matricule)."""
        ll = ligne.lower()
        # Matricule TVA (TVA: 1234567...), téléphone, fax, adresse
        if re.search(r'tva\s*:\s*\d{5,}', ll):
            return True
        if re.search(r't[ée]l|fax|adress|mail|@|www\.|\.com|\.tn', ll):
            return True
        if re.search(r'\d{8,}', ll) and not re.search(r'[,.]', ll):
            # Ligne avec un long nombre sans virgule/point = probablement téléphone
            return True
        if re.search(r'scann|sign|cachet|facture\s+[àa]\s+la\s+somm', ll):
            return True
        return False

    # Marquer les lignes déjà matchées par un label plus spécifique
    lignes_matchees = set()

    # Phase 1 : chercher label + nombre sur la même ligne
    for i, ligne in enumerate(lignes):
        ll = ligne.lower().strip()
        if not ll or _est_ligne_footer(ligne):
            continue
        for champ, pat in label_patterns_ordered:
            if champ in resultats:
                continue
            if re.search(pat, ll, re.IGNORECASE):
                lignes_matchees.add(i)
                nombre = _extraire_nombre_tunisien(ligne)
                if _est_montant_valide(nombre):
                    resultats[champ] = nombre
                break

    # Phase 2 : pour les labels trouvés SANS nombre, chercher dans les lignes voisines
    for i, ligne in enumerate(lignes):
        ll = ligne.lower().strip()
        if not ll or _est_ligne_footer(ligne):
            continue
        for champ, pat in label_patterns_ordered:
            if champ in resultats:
                continue
            if re.search(pat, ll, re.IGNORECASE):
                # Chercher le nombre le plus proche (±5 lignes)
                for delta in [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]:
                    j = i + delta
                    if 0 <= j < len(lignes) and not _est_ligne_footer(lignes[j]):
                        nombre = _extraire_nombre_tunisien(lignes[j])
                        if _est_montant_valide(nombre):
                            resultats[champ] = nombre
                            break
                break

    # Phase 3 : heuristique par taille si montant_ttc manquant
    # Collecter tous les nombres > 10 du texte (hors footer)
    if "montant_ttc" not in resultats:
        tous_nombres = []
        for ligne in lignes:
            if _est_ligne_footer(ligne):
                continue
            nums = _extraire_tous_nombres_tunisiens(ligne)
            tous_nombres.extend(n for n in nums if _est_montant_valide(n))

        # Dédupliquer (± 1)
        tous_nombres = sorted(set(tous_nombres), reverse=True)
        # Le plus grand nombre > 100 est probablement le TTC
        gros = [n for n in tous_nombres if n > 100]

        if gros and "montant_ttc" not in resultats:
            resultats["montant_ttc"] = gros[0]
        if len(gros) >= 2 and "montant_ht" not in resultats:
            resultats["montant_ht"] = gros[1]
        if "montant_ttc" in resultats and "montant_ht" in resultats and "montant_tva" not in resultats:
            tva = round(resultats["montant_ttc"] - resultats["montant_ht"], 3)
            if tva > 0:
                resultats["montant_tva"] = tva

    # Utiliser FODEC pour estimer HT si manquant (FODEC = 1% de HT)
    if "_fodec" in resultats and "montant_ht" not in resultats:
        ht_estime = round(resultats["_fodec"] / 0.01, 3)
        if ht_estime > 100:
            resultats["montant_ht"] = ht_estime

    # Nettoyer les champs intermédiaires
    resultats.pop("_fodec", None)
    resultats.pop("_timbre", None)
    resultats.pop("_base_tva", None)

    return resultats


def _extraire_nombre_tunisien(ligne):
    """
    Extrait le dernier nombre significatif d'une ligne (format tunisien).
    Format : espace/point = milliers, virgule = décimal.
    Exemples: '1 438,128' → 1438.128, '275,977' → 275.977, '1729,486' → 1729.486
    """
    nums = _extraire_tous_nombres_tunisiens(ligne)
    return nums[-1] if nums else None


def _extraire_tous_nombres_tunisiens(ligne):
    """
    Extrait tous les nombres d'une ligne (format tunisien).
    Espace/point = séparateur milliers (groupes de 3), virgule = décimal.
    """
    resultats = []
    # Pattern : chiffres avec éventuels espaces/points, puis optionnellement virgule + décimales
    matches = re.findall(r'(\d[\d\s.]*(?:,\d{1,3})?)', ligne)
    for m in matches:
        s = m.strip().rstrip(".")
        if not s or not any(c.isdigit() for c in s):
            continue

        # Séparer la partie avant/après la virgule
        if "," in s:
            avant_virgule, apres_virgule = s.split(",", 1)
        else:
            avant_virgule, apres_virgule = s, None

        # Valider les groupes de milliers (espaces)
        groupes = avant_virgule.strip().split()
        if len(groupes) > 1:
            # Chaque groupe après le premier doit avoir exactement 3 chiffres
            valide = all(re.fullmatch(r'\d{3}', g.replace(".", "")) for g in groupes[1:])
            if not valide:
                # Les espaces ne sont PAS des séparateurs de milliers → prendre le dernier groupe
                avant_virgule = groupes[-1]
            else:
                avant_virgule = "".join(g.replace(" ", "") for g in groupes)

        # Supprimer les espaces restants
        avant_virgule = re.sub(r'\s+', '', avant_virgule)

        # Gérer les points comme séparateurs de milliers
        if "." in avant_virgule:
            parties = avant_virgule.split(".")
            if len(parties) > 2:
                # 1.452.509 → 1452509 (tous des milliers sauf si apres_virgule absent)
                avant_virgule = "".join(parties)
            elif len(parties) == 2 and len(parties[1]) == 3 and apres_virgule is None:
                # 452.509 sans virgule → ambigu, garder comme décimal
                pass
            elif len(parties) == 2 and apres_virgule is not None:
                # 1.452,509 → 1452,509
                avant_virgule = "".join(parties)

        if apres_virgule is not None:
            s = avant_virgule + "." + apres_virgule
        else:
            s = avant_virgule

        try:
            v = float(s)
            if v > 0.5:
                resultats.append(v)
        except ValueError:
            continue

    return resultats


def _fusionner_montants(r1, r2):
    """
    Fusionne les résultats de deux passes OCR en choisissant les valeurs
    les plus cohérentes (TTC > HT > TVA, TTC ≈ HT + TVA).
    """
    candidats = {}
    for champ in ("montant_ht", "montant_tva", "montant_ttc"):
        vals = []
        if champ in r1:
            vals.append(r1[champ])
        if champ in r2 and (champ not in r1 or abs(r2[champ] - r1[champ]) > 0.01):
            vals.append(r2[champ])
        if vals:
            candidats[champ] = vals

    resultats = {}

    # TTC : prendre le plus grand candidat raisonnable
    if "montant_ttc" in candidats:
        resultats["montant_ttc"] = max(candidats["montant_ttc"])

    # HT : prendre le candidat < TTC le plus grand
    if "montant_ht" in candidats:
        ttc = resultats.get("montant_ttc", float("inf"))
        valides = [v for v in candidats["montant_ht"] if v < ttc]
        if valides:
            resultats["montant_ht"] = max(valides)
        else:
            resultats["montant_ht"] = min(candidats["montant_ht"])

    # TVA : prendre le candidat le plus cohérent avec TTC - HT
    if "montant_tva" in candidats:
        ttc = resultats.get("montant_ttc")
        ht = resultats.get("montant_ht")
        if ttc and ht:
            tva_attendue = ttc - ht
            meilleur = min(candidats["montant_tva"], key=lambda v: abs(v - tva_attendue))
            resultats["montant_tva"] = meilleur
        else:
            resultats["montant_tva"] = candidats["montant_tva"][0]

    # Si TVA manquante mais HT et TTC présents, calculer
    if "montant_tva" not in resultats and "montant_ttc" in resultats and "montant_ht" in resultats:
        tva = round(resultats["montant_ttc"] - resultats["montant_ht"], 3)
        if tva > 0:
            resultats["montant_tva"] = tva

    return resultats


# ──────────────────────────────────────────────────────────────────────
# CORRECTION EXTENSION
# ──────────────────────────────────────────────────────────────────────

def _corriger_extension(nom, ct, contenu):
    ext = os.path.splitext(nom)[1].lower()
    if ext in [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]:
        return nom, ext

    map_ct = {
        "image/jpeg":       ".jpg",
        "image/jpg":        ".jpg",
        "image/png":        ".png",
        "image/tiff":       ".tiff",
        "image/bmp":        ".bmp",
        "application/pdf":  ".pdf",
    }
    ct_clean = (ct or "").split(";")[0].strip()
    if ct_clean in map_ct:
        return nom + map_ct[ct_clean], map_ct[ct_clean]

    if contenu:
        sig = contenu[:8]
        if sig[:4] == b'\x89PNG':  return nom + ".png",  ".png"
        if sig[:2] == b'\xff\xd8': return nom + ".jpg",  ".jpg"
        if sig[:4] == b'%PDF':     return nom + ".pdf",  ".pdf"
        if sig[:2] == b'BM':       return nom + ".bmp",  ".bmp"

    return nom, ext if ext else ""