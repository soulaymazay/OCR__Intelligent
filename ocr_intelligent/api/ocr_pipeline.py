"""
ocr_pipeline.py - Groupe Bayoudh Metal

CORRECTIONS v3 :
  1. Seuil mots minimum : 3 → 1 (évite rejet des petites images valides)
  2. Normalisation nom fichier : suppression du hash Frappe (ex: facturebba7b3.png → facture.png)
  3. Recherche OCR Document tolérante : nom exact + nom sans hash + nom de base
  4. Message d'erreur plus précis avec taille fichier conseillée
  5. Statuts alignés sur les options du doctype OCR Document
"""

import frappe
import os
import re
import json
import time
import random
from werkzeug.utils import secure_filename


# ──────────────────────────────────────────────────────────────────────
# UTILITAIRE : Nettoyage du nom de fichier (hash Frappe)
# ──────────────────────────────────────────────────────────────────────

def _nettoyer_nom_frappe(nom):
    """
    Frappe ajoute un hash hexadécimal au nom des fichiers uploadés.
    Ex : 'facturebba7b3.png' → 'facture.png'
         'cheque_test_a1b2c3.jpg' → 'cheque_test.jpg'

    Cette fonction supprime ce hash pour retrouver le nom original.
    """
    base, ext = os.path.splitext(nom)
    # Hash Frappe : 6 à 10 caractères hexadécimaux en fin de nom
    base_nettoye = re.sub(r'[_\-]?[a-f0-9]{6,10}$', '', base, flags=re.IGNORECASE)
    if base_nettoye and base_nettoye != base:
        return base_nettoye + ext
    return nom


def _variantes_nom(nom_fichier):
    """
    Génère toutes les variantes du nom de fichier pour la recherche.
    Retourne une liste ordonnée du plus spécifique au plus général.
    """
    variantes = [nom_fichier]  # nom exact en premier

    # Variante sans hash Frappe
    nom_nettoye = _nettoyer_nom_frappe(nom_fichier)
    if nom_nettoye != nom_fichier:
        variantes.append(nom_nettoye)

    # Variante nom de base sans extension
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
def pipeline_complet():
    """
    POST /api/method/ocr_intelligent.api.ocr_pipeline.pipeline_complet
    Body → form-data → clé: file | type: File
    """
    files = frappe.request.files
    if not files or "file" not in files:
        return {"success": False, "erreur": "Aucun fichier reçu (form-data, clé: 'file')."}

    file_obj     = files["file"]
    nom_original = file_obj.filename
    nom_original = secure_filename(nom_original)
    content_type = getattr(file_obj, "content_type", "") or ""
    contenu      = file_obj.read()

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

        # ── FIX 2 : seuil abaissé de 3 → 1 mot ───────────────────────
        mots = [m for m in (texte_brut or "").split() if len(m) > 1]

        if len(mots) < 1:
            return {
                "success":         False,
                "score_confiance": score,
                "texte_extrait":   texte_brut[:500] if texte_brut else "",
                "taille_fichier":  f"{taille_kb:.1f} KB",
                "erreur": (
                    f"Aucun texte détecté (score OCR {score}%, 0 mot). "
                    f"Conseils : "
                    f"(1) Utilisez une image de bonne qualité (min. 50 KB, 300 DPI). "
                    f"(2) Assurez-vous que le document contient du texte imprimé visible. "
                    f"(3) Évitez les images floues, trop sombres ou trop petites."
                )
            }

        # Avertissement si peu de mots (1-2) mais on continue quand même
        avertissement_mots = None
        if len(mots) < 3:
            avertissement_mots = (
                f"Attention : seulement {len(mots)} mot(s) détecté(s). "
                "Résultats potentiellement incomplets."
            )

        # ── Extraction + Validation ────────────────────────────────────
        from ocr_intelligent.ocr.extractor  import ExtracteurIntelligent
        from ocr_intelligent.ocr.validator  import Validateur

        extracteur     = ExtracteurIntelligent(texte_brut)
        donnees        = extracteur.extraire_tout()
        type_doc       = donnees["type_document"]
        champs_bruts   = donnees["champs"]
        validateur     = Validateur(type_doc, champs_bruts)
        rapport        = validateur.valider()
        champs_valides = rapport["champs_valides"]
        erreurs_valid  = rapport["erreurs"]

        # ── Statut aligné sur les options du doctype ───────────────────
        statut_map = {
            "valide":             "Validé",
            "validation_requise": "Validation requise",
            "avertissement":      "Validation requise",
        }
        statut = statut_map.get(rapport["statut"], "En attente")

        # ── FIX 1 : recherche OCR Document avec variantes de nom ───────
        # Frappe modifie le nom du fichier en ajoutant un hash (ex: facturebba7b3.png)
        # On cherche avec toutes les variantes possibles
        ocr_doc_name = None
        variantes    = _variantes_nom(nom_fichier)

        for variante in variantes:
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
            # Mettre à jour le document existant
            frappe.db.set_value("OCR Document", ocr_doc_name, {
                "extracted_text":  texte_brut,
                "extracted_field": json.dumps(champs_valides, ensure_ascii=False, indent=2),
                "confidence_score": score,
                "status":          statut,
            })
        else:
            # Créer un nouveau document
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

        # ── Matching champ par champ ───────────────────────────────────
        # Essayer d'abord avec le nom original, puis avec les variantes
        res_match = None
        for variante in variantes:
            res_match = frappe.call(
                "ocr_intelligent.api.field_matcher.trouver_meilleur_candidat",
                nom_fichier=variante
            )
            if res_match.get("success"):
                break

        if not res_match or not res_match.get("success"):
            reponse_erreur = {
                "success":         False,
                "nom_fichier":     nom_fichier,
                "ocr_document_id": ocr_doc_name,
                "score_confiance": score,
                "texte_extrait":   texte_brut[:300],
                "nb_candidats":    res_match.get("nb_candidats", 0) if res_match else 0,
                "iterations":      res_match.get("iterations", []) if res_match else [],
            }
            if avertissement_mots:
                reponse_erreur["avertissement"] = avertissement_mots
            if res_match:
                reponse_erreur.update({
                    "erreur":         res_match.get("erreur"),
                    "champ_bloquant": res_match.get("champ_bloquant"),
                    "conseil":        res_match.get("conseil"),
                })
            return reponse_erreur

        champs_remplis = res_match.get("champs_remplis", {})

        reponse_ok = {
            "success":             True,
            "nom_fichier":         nom_fichier,
            "type_document":       res_match.get("type_document"),
            "champs_remplis":      champs_remplis,
            "erreurs_validation":  erreurs_valid,
            "score_confiance":     score,
            "nombre_pages":        nb_pages,
            "methode_ocr":         methode,
            "psm_selectionne":     psm,
            "debug_pretraitement": debug_info,
            "ocr_document_id":     ocr_doc_name,
            "candidat_choisi":     res_match.get("candidat_choisi"),
            "score_final":         res_match.get("score_final"),
            "nb_candidats":        res_match.get("nb_candidats"),
            "iterations":          res_match.get("iterations", []),
            "texte_extrait":       texte_brut[:500],
            "message": (
                f"{len(champs_remplis)} champ(s) rempli(s) "
                f"(compatibilité {round((res_match.get('score_final', 0)) * 100)}%)"
            )
        }
        if avertissement_mots:
            reponse_ok["avertissement"] = avertissement_mots

        return reponse_ok

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