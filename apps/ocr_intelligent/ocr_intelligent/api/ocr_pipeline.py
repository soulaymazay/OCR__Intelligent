# -*- coding: utf-8 -*-
"""
ocr_pipeline.py - Groupe Bayoudh Metal

CORRECTIONS v5 (TVA multi-taux) :
  ─ _parser_montants :
      • Dédoublonnage correct des montants TVA candidats avant sommation.
        Avant v5 : sum(set(...)) sur des flottants → doublons mal éliminés.
        Après v5  : round(v,3) avant set() → 15.21 + 186.90 = 202.11 ✓

      • Recalcul HT APRÈS agrégation TVA multi-taux (correction principale).
        Avant v4 : le recalcul HT = TTC - TVA était fait avant l'agrégation
                   multi-taux → HT restait à 0 si TVA était actualisée après.
        Après v5  : second recalcul garanti en fin de fonction → HT = 1413.22 - 202.11 = 1211.11 ✓

  ─ _nettoyer_coherence_montants :
      • Recalcul HT ajouté pour le cas TVA multi-taux (ht <= 0 mais ttc et tva connus).
      • Règle de suppression HT assouplie : on ne supprime montant_ht que
        si TVA est absente (ht ≈ ttc sans TVA connue = incohérent).

  ─ _executer_pipeline_job :
      • Copie des clés OCR brutes (montant_ht, montant_tva, montant_ttc)
        dans champs_remplis comme fallback pour ocr_form.js (Priorité 2).

  ─ Toutes les corrections v2, v3 et v4 sont conservées.
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
    "echeance": "date_echeance",
    "validite": "date_echeance",
    "tva":      "montant_tva",
    "montant_total": "montant_ttc",
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


def _parse_date_cheque_global(texte_src, formats, annee_min=2018, annee_max=None):
    from datetime import datetime as _dt, timedelta as _td

    t = texte_src or ""
    t = t.replace("／", "/").replace("⁄", "/")
    t = re.sub(r"[‐‑‒–—−]", "-", t)
    t = re.sub(r"[Oo](?=[\d/\-.])", "0", t)
    t = re.sub(r"(?<=[\d/\-.])[Oo]", "0", t)
    t = re.sub(r"\bl(?=\d)", "1", t)
    t = re.sub(r"\bI(?=\d)", "1", t)
    t = re.sub(r"(?<=\d)\s*[|\\]\s*(?=\d)", "/", t)

    if annee_max is None:
        annee_max = _dt.now().year + 1

    d_sep = r"\d{1,2}[\s]*[\/\-\.][\s]*\d{1,2}[\s]*[\/\-\.][\s]*\d{2,4}"
    candidats = set()

    for d in re.findall(r"(" + d_sep + r")", t):
        d = re.sub(r"[\s]*([\/\-\.])[\s]*", r"\1", d.strip())
        candidats.add(d)

    for m in re.finditer(r"(?i)\b(?:date|tunis\s*,?\s*le|\ble\b)\b.{0,40}?(" + d_sep + r")", t):
        d = re.sub(r"[\s]*([\/\-\.])[\s]*", r"\1", m.group(1).strip())
        candidats.add(d)

    dates_valides = []
    for cand in candidats:
        cand = cand.strip().strip("[]{}()|:;,")
        for fmt in formats:
            try:
                obj = _dt.strptime(cand, fmt)
                if not (annee_min <= obj.year <= annee_max):
                    continue
                if obj > _dt.now() + _td(days=30):
                    continue
                dates_valides.append(obj)
                break
            except ValueError:
                continue
    return dates_valides


def _extraire_textes_date_cheque_image_global(chemin_img):
    textes = []
    try:
        ext = os.path.splitext(chemin_img)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_img, dpi=300)
            if not pages:
                return textes
            img = cv2.cvtColor(np.array(pages[0].convert("RGB")), cv2.COLOR_RGB2BGR)
        else:
            img = cv2.imread(chemin_img)
            if img is None:
                pil = PILImage.open(chemin_img).convert("RGB")
                img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

        h, w = img.shape[:2]
        zones = [
            (0.45, 0.00, 1.00, 0.35),
            (0.30, 0.00, 1.00, 0.45),
            (0.00, 0.00, 1.00, 0.28),
        ]

        for x0p, y0p, x1p, y1p in zones:
            x0, y0 = int(w * x0p), int(h * y0p)
            x1, y1 = int(w * x1p), int(h * y1p)
            crop = img[y0:y1, x0:x1]
            if crop is None or crop.size == 0:
                continue

            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            ch, cw = gray.shape[:2]
            if cw < 1600:
                scale = 1600 / max(cw, 1)
                gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

            thr_adapt = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 12
            )
            thr_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

            for arr in (gray, thr_adapt, thr_otsu):
                pil_arr = PILImage.fromarray(arr)
                for psm in (6, 7, 11, 12):
                    txt = pytesseract.image_to_string(
                        pil_arr, lang="fra+eng", config=f"--oem 3 --psm {psm}"
                    )
                    if txt and txt.strip():
                        textes.append(txt)
    except Exception:
        pass
    return textes


# ──────────────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ──────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def pipeline_complet(file_url="", source_doctype="", payment_method=""):
    import uuid

    contenu      = None
    nom_original = None
    content_type = ""

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
            return {"success": False, "erreur": "Fichier introuvable : {}".format(file_url)}

        nom_original = os.path.basename(chemin_fichier)
        with open(chemin_fichier, "rb") as f:
            contenu = f.read()
    else:
        files = frappe.request.files
        if not files or "file" not in files:
            return {"success": False, "erreur": "Aucun fichier reçu (form-data, clé: 'file')."}
        file_obj     = files["file"]
        nom_original = file_obj.filename
        content_type = getattr(file_obj, "content_type", "") or ""
        contenu      = file_obj.read()

    nom_original = secure_filename(nom_original)

    taille_kb = len(contenu) / 1024
    if taille_kb < 2:
        return {
            "success": False,
            "erreur": (
                "Fichier trop petit ({:.1f} KB). "
                "Une facture/document lisible fait généralement entre 50 KB et 5 MB. "
                "Vérifiez que le fichier contient bien une image et non une icône.".format(taille_kb)
            )
        }

    nom_fichier, ext = _corriger_extension(nom_original, content_type, contenu)
    extensions_ok    = [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]
    if ext not in extensions_ok:
        return {
            "success": False,
            "erreur": "Format '{}' non supporté. Acceptés : {}".format(ext, ', '.join(extensions_ok))
        }

    dossier_tmp = os.path.join(frappe.get_site_path(), "private", "files")
    os.makedirs(dossier_tmp, exist_ok=True)
    job_token  = str(uuid.uuid4()).replace("-", "")
    chemin_tmp = os.path.join(dossier_tmp, "ocr_tmp_{}_{}".format(job_token, nom_fichier))

    with open(chemin_tmp, "wb") as f:
        f.write(contenu)

    frappe.cache().set_value(
        "ocr_pipeline_status_{}".format(job_token), "en_cours", expires_in_sec=3600
    )

    frappe.enqueue(
        "ocr_intelligent.api.ocr_pipeline._executer_pipeline_job",
        queue="long",
        timeout=600,
        chemin_tmp=chemin_tmp,
        nom_fichier=nom_fichier,
        ext=ext,
        source_doctype=source_doctype,
        payment_method=payment_method,
        uploaded_by=frappe.session.user,
        job_token=job_token,
    )

    return {"success": True, "async": True, "job_id": job_token}


@frappe.whitelist()
def get_ocr_statut(job_id):
    status = frappe.cache().get_value("ocr_pipeline_status_{}".format(job_id))

    if status == "termine":
        result_raw = frappe.cache().get_value("ocr_pipeline_result_{}".format(job_id))
        if result_raw is None:
            return {
                "status": "termine",
                "result": {"success": False, "erreur": "Résultat expiré du cache."},
            }
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except (ValueError, TypeError):
                result = {"success": False, "erreur": result_raw}
        else:
            result = result_raw
        return {"status": "termine", "result": result}

    if status == "erreur":
        erreur_raw = frappe.cache().get_value("ocr_pipeline_erreur_{}".format(job_id))
        if isinstance(erreur_raw, str):
            try:
                erreur = json.loads(erreur_raw)
            except (ValueError, TypeError):
                erreur = erreur_raw
        else:
            erreur = erreur_raw or "Erreur inconnue."
        return {"status": "erreur", "erreur": erreur}

    if status == "en_cours":
        return {"status": "en_cours"}

    return {"status": "inconnu"}


def _executer_pipeline_job(chemin_tmp, nom_fichier, ext,
                            source_doctype, uploaded_by, job_token,
                            payment_method=""):
    try:
        from ocr_intelligent.ocr.ocr_engine import get_engine
        engine = get_engine()
        try:
            res_ocr = engine.extraire_texte(chemin_tmp)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "OCR Pipeline")
            _stocker_erreur(job_token, "Erreur OCR : {}".format(str(e)))
            return

        texte_brut = res_ocr.get("texte", "")
        score      = res_ocr.get("score_confiance", 0)
        nb_pages   = res_ocr.get("nombre_pages", 1)
        methode    = res_ocr.get("methode", "—")
        psm        = res_ocr.get("psm_selectionne", "—")

        mots = [m for m in (texte_brut or "").split() if len(m) > 1]

        if len(mots) < 1:
            _stocker_resultat(job_token, {
                "success":         False,
                "score_confiance": score,
                "texte_extrait":   texte_brut[:500] if texte_brut else "",
                "erreur": (
                    "Aucun texte détecté (score OCR {}%, 0 mot). "
                    "Utilisez une image de bonne qualité (min. 50 KB, 300 DPI).".format(score)
                )
            })
            return

        from ocr_intelligent.ocr.payment_doc_extractor import _normaliser_payment_method as _norm_pm
        _is_payment = (
            source_doctype == "Payment Entry"
            or bool(payment_method and _norm_pm(payment_method))
        )
        if _is_payment:
            _executer_pipeline_payment_entry(
                chemin_tmp=chemin_tmp,
                texte_brut=texte_brut,
                score=score,
                payment_method=payment_method or "",
                nom_fichier=nom_fichier,
                uploaded_by=uploaded_by,
                job_token=job_token,
            )
            return

        from ocr_intelligent.ocr.header_extractor import extraire_champs_entete
        from ocr_intelligent.ocr.nlp_analyzer     import analyser_contexte
        from ocr_intelligent.api.field_matcher    import MAPPING_CHAMPS

        lignes       = texte_brut.splitlines()
        texte_entete = "\n".join(lignes[:min(60, len(lignes))])

        if len(texte_entete.split()) < 3:
            texte_entete = texte_brut

        resultat_entete = extraire_champs_entete(texte_entete)
        type_doc        = resultat_entete["type_document"]

        _LABELS_REJETES = {
            "cheque":        "chèque / virement",
            "traite":        "traite (Lettre de Change)",
            "bon_livraison": "bon de livraison",
            "devis":         "devis / proforma",
            "bon_commande":  "bon de commande",
            "facture_vente": "facture de vente",
            "invoice_vente": "facture de vente",
        }

        TYPES_REJETES = {
            "cheque", "traite", "bon_livraison", "devis",
            "bon_commande", "facture_vente", "invoice_vente"
        }
        _MSG_CONTEXTE = "une facture d'achat"

        _SIGNAUX_FACTURE_VENTE = [
            "client", "customer", "sold to", "vendu à", "à facturer à",
            "adresse de livraison client", "billing address", "adresse de facturation client",
        ]

        _SIGNAUX_FACTURE = [
            "facture", "invoice", "à payer", "net à payer",
            "tva", "ttc", "ht", "montant ttc", "échéance",
            "règlement", "facturé", "bill to", "vendu à",
            "sold to", "facture n°", "fact n°",
            "chèque", "cheque", "virement", "paiement", "règlement",
            "montant payé", "référence paiement",
        ]

        if any(s in texte_brut.lower() for s in _SIGNAUX_FACTURE_VENTE):
            _stocker_resultat(job_token, {
                "success": False,
                "type_document": "facture_vente",
                "erreur": (
                    "Document refusé : ce fichier est une facture de vente, "
                    "pas une facture d'achat. "
                    "Veuillez scanner une facture fournisseur (facture d'achat)."
                ),
            })
            return

        if not any(s in texte_brut.lower() for s in _SIGNAUX_FACTURE):
            _stocker_resultat(job_token, {
                "success": False,
                "type_document": "inconnu",
                "erreur": (
                    "Document refusé : aucun indicateur de facturation détecté. "
                    "Veuillez scanner une facture fournisseur."
                ),
            })
            return

        if type_doc in TYPES_REJETES:
            _stocker_resultat(job_token, {
                "success": False,
                "type_document": type_doc,
                "erreur": (
                    "Document refusé : ce fichier ressemble à un {}, "
                    "pas à {}. "
                    "Veuillez scanner le bon document.".format(
                        _LABELS_REJETES.get(type_doc, type_doc),
                        _MSG_CONTEXTE
                    )
                ),
            })
            return

        champs_valides = {}
        for champ, valeur in resultat_entete["champs"].items():
            if valeur is not None and str(valeur).strip():
                champ_norm = _NORMALISE_CHAMPS.get(champ, champ)
                champs_valides[champ_norm] = valeur

        champs_critiques = ["numero_facture", "date", "fournisseur"]
        champs_manquants = [c for c in champs_critiques if c not in champs_valides]
        if champs_manquants and texte_entete != texte_brut:
            resultat_complet = extraire_champs_entete(texte_brut)
            if resultat_complet["type_document"] != "inconnu":
                type_doc = resultat_complet["type_document"]
            for champ, valeur in resultat_complet["champs"].items():
                champ_norm = _NORMALISE_CHAMPS.get(champ, champ)
                if valeur and champ_norm not in champs_valides:
                    champs_valides[champ_norm] = valeur

        champs_cles = ["numero_facture", "date", "fournisseur", "date_echeance"]
        manquants_apres_texte = [c for c in champs_cles if c not in champs_valides]
        if manquants_apres_texte:
            champs_image = _extraire_champs_entete_image(chemin_tmp)
            for champ_img, valeur_img in (champs_image or {}).items():
                champ_norm = _NORMALISE_CHAMPS.get(champ_img, champ_img)
                if valeur_img and champ_norm not in champs_valides:
                    champs_valides[champ_norm] = valeur_img

        montants_image = _extraire_montants_image(chemin_tmp)
        for champ_m, val_m in montants_image.items():
            if val_m and val_m > 0:
                val_existante = champs_valides.get(champ_m)
                if _doit_remplacer_montant(val_existante, float(val_m)):
                    champs_valides[champ_m] = str(round(val_m, 3))

        analyse_nlp = analyser_contexte(
            texte             = texte_brut,
            champs_regex      = champs_valides,
            champs_formulaire = {},
            type_doc_force    = type_doc if type_doc != "inconnu" else None,
        )

        for champ, meta in analyse_nlp["champs_enrichis"].items():
            if champ not in champs_valides and meta.get("confiance", 0) >= 0.4:
                champs_valides[champ] = meta["valeur"]

        _nettoyer_coherence_montants(champs_valides)

        if type_doc == "inconnu":
            indices_facture = [
                "numero_facture", "montant_ttc", "montant_ht",
                "montant_tva", "date_echeance", "fournisseur",
            ]
            nb_indices = sum(1 for k in indices_facture if champs_valides.get(k))
            if nb_indices >= 3:
                type_doc = "facture"

        if type_doc == "inconnu" and analyse_nlp["type_document"] != "inconnu":
            type_doc = analyse_nlp["type_document"]

        if type_doc in TYPES_REJETES:
            _stocker_resultat(job_token, {
                "success": False,
                "type_document": type_doc,
                "erreur": (
                    "Document refusé : ce fichier ressemble à un {}, "
                    "pas à {}. "
                    "Veuillez scanner le bon document.".format(
                        _LABELS_REJETES.get(type_doc, type_doc),
                        _MSG_CONTEXTE
                    )
                ),
            })
            return

        if type_doc == "inconnu":
            if not any(s in texte_brut.lower() for s in _SIGNAUX_FACTURE):
                _stocker_resultat(job_token, {
                    "success": False,
                    "type_document": "inconnu",
                    "erreur": (
                        "Document refusé : ce fichier ne ressemble pas à une facture d'achat. "
                        "Aucun indicateur de facturation détecté (facture, TVA, TTC, HT…). "
                        "Veuillez scanner une facture fournisseur."
                    ),
                })
                return

        mapping        = MAPPING_CHAMPS.get(type_doc, MAPPING_CHAMPS["inconnu"])
        champs_remplis = {}
        for champ_ocr, fieldname_frappe in mapping.items():
            valeur = champs_valides.get(champ_ocr)
            if valeur:
                champs_remplis[fieldname_frappe] = valeur

        if "bill_date" in champs_remplis and "posting_date" not in champs_remplis:
            champs_remplis["posting_date"] = champs_remplis["bill_date"]

        # Copier aussi les cles OCR brutes dans champs_remplis.
        # Fallback pour ocr_form.js Priorité 2 : si montant_ht/tva/ttc sont
        # dans champs_valides mais pas dans champs_remplis (mapping non appliqué),
        # les copier directement pour que le JS puisse les trouver.
        for ocr_key in ("montant_ht", "montant_tva", "montant_ttc"):
            if ocr_key not in champs_remplis and champs_valides.get(ocr_key):
                champs_remplis[ocr_key] = champs_valides[ocr_key]

        numero_facture_ocr = champs_remplis.get("bill_no") or champs_valides.get("numero_facture")
        if numero_facture_ocr:
            doublon = frappe.db.get_value(
                "Purchase Invoice",
                {"bill_no": str(numero_facture_ocr).strip(), "docstatus": ["!=", 2]},
                ["name", "bill_date", "posting_date"],
                as_dict=True,
            )
            if doublon:
                date_import = doublon.get("bill_date") or doublon.get("posting_date") or ""
                if date_import:
                    try:
                        from datetime import datetime as _dt
                        date_import = _dt.strptime(str(date_import), "%Y-%m-%d").strftime("%d/%m/%Y")
                    except Exception:
                        date_import = str(date_import)
                _stocker_resultat(job_token, {
                    "success":         True,
                    "doublon":         True,
                    "doublon_name":    doublon["name"],
                    "doublon_date":    date_import,
                    "numero_facture":  numero_facture_ocr,
                    "champs_remplis":  champs_remplis,
                    "type_document":   type_doc,
                    "score_confiance": score,
                })
                return

        statut = "Validé" if len(champs_remplis) >= 3 else "Validation requise"
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
                "uploaded_by":      uploaded_by,
                "confidence_score": score,
                "extracted_text":   texte_brut,
                "extracted_field":  json.dumps(champs_valides, ensure_ascii=False, indent=2),
                "status":           statut,
            })
            ocr_doc.insert(ignore_permissions=True)
            ocr_doc_name = ocr_doc.name

        frappe.db.commit()

        if not champs_remplis:
            _stocker_resultat(job_token, {
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
                    "Document analysé (type détecté : {}) "
                    "mais aucun champ n'a pu être extrait depuis l'en-tête. "
                    "Vérifiez la qualité du document.".format(type_doc)
                ),
                "conseil": "Préférez un PDF natif ou une image nette ≥ 300 DPI.",
            })
            return

        _stocker_resultat(job_token, {
            "success":              True,
            "nom_fichier":          nom_fichier,
            "type_document":        type_doc,
            "champs_remplis":       champs_remplis,
            "score_confiance":      score,
            "nombre_pages":         nb_pages,
            "methode_ocr":          methode,
            "ocr_document_id":      ocr_doc_name,
            "texte_extrait":        texte_brut[:500],
            "score_type_document":  analyse_nlp.get("score_type", 0),
            "champs_enrichis": {
                champ: meta["valeur"]
                for champ, meta in analyse_nlp.get("champs_enrichis", {}).items()
            },
            "champs_compatibles":   analyse_nlp.get("champs_compatibles", []),
            "champs_incompatibles": analyse_nlp.get("champs_incompatibles", []),
            "entites_nlp":          analyse_nlp.get("entites", {}),
            "suggestion_nlp":       analyse_nlp.get("suggestion", ""),
            "message": (
                "{} champ(s) rempli(s) depuis l'en-tête "
                "(type : {}, score OCR : {}%, "
                "NLP : {} compatible(s), "
                "{} incompatible(s))".format(
                    len(champs_remplis),
                    type_doc,
                    score,
                    analyse_nlp.get('nb_compatibles', 0),
                    analyse_nlp.get('nb_incompatibles', 0)
                )
            ),
        })

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Pipeline Job Error")
        _stocker_erreur(job_token, "Erreur inattendue : {}".format(str(e)))

    finally:
        if os.path.exists(chemin_tmp):
            try:
                os.remove(chemin_tmp)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────

def _est_montant_nul(valeur_str: str) -> bool:
    try:
        return float(str(valeur_str).replace(",", ".").replace(" ", "")) == 0
    except Exception:
        return False


def _parse_montant_float(valeur_str: str):
    try:
        propre = _nettoyer_montant_ocr(str(valeur_str)).replace(" ", "")
        if "," in propre and "." not in propre:
            propre = propre.replace(",", ".")
        return float(propre)
    except Exception:
        return None


def _doit_remplacer_montant(valeur_existante, valeur_nouvelle: float) -> bool:
    if valeur_nouvelle is None or valeur_nouvelle <= 0:
        return False

    if not valeur_existante or _est_montant_nul(valeur_existante):
        return True

    ancienne = _parse_montant_float(valeur_existante)
    if ancienne is None or ancienne <= 0:
        return True

    ratio = max(ancienne, valeur_nouvelle) / max(min(ancienne, valeur_nouvelle), 0.001)
    return ratio >= 20


def _nettoyer_coherence_montants(champs_valides: dict) -> None:
    ttc = _parse_montant_float(champs_valides.get("montant_ttc"))
    ht  = _parse_montant_float(champs_valides.get("montant_ht"))
    tva = _parse_montant_float(champs_valides.get("montant_tva"))

    # Ne supprimer TVA que si elle est >= 90% du TTC.
    # (TVA égale au TTC = aberrant), indépendamment du HT.
    if ttc and tva and tva >= ttc * 0.90:
        champs_valides.pop("montant_tva", None)
        tva = None

    # Ne supprimer HT que si TVA est absente.
    # Si TVA est connue et HT ≈ TTC, c'est peut-être une erreur OCR sur HT,
    # mais on ne peut pas trancher → on garde HT pour ne pas perdre la donnée.
    # On supprime HT uniquement si pas de TVA connue ET HT ≈ TTC (= HT mal parsé).
    if ttc and ht and ht >= ttc * 0.98 and not tva:
        champs_valides.pop("montant_ht", None)
        ht = None

    if ttc and ht and ht < ttc * 0.25 and not tva:
        champs_valides.pop("montant_ht", None)
        ht = None

    # Relire après nettoyage
    ttc = _parse_montant_float(champs_valides.get("montant_ttc"))
    ht  = _parse_montant_float(champs_valides.get("montant_ht"))
    tva = _parse_montant_float(champs_valides.get("montant_tva"))

    # ── Recalcul croisé ──────────────────────────────────────────────
    # Cas TVA multi-taux : HT = 0 ou absent mais TVA et TTC connus
    if ttc and tva and (not ht or ht <= 0):
        ht_calc = round(ttc - tva, 3)
        if ht_calc > 0:
            champs_valides["montant_ht"] = str(ht_calc)
            ht = ht_calc

    # Compléter HT depuis TTC - TVA
    if ttc and tva and not ht:
        ht_calc = round(ttc - tva, 3)
        if ht_calc > 0:
            champs_valides["montant_ht"] = str(ht_calc)

    # Compléter TVA depuis TTC - HT
    if ttc and ht and not tva:
        tva_calc = round(ttc - ht, 3)
        if tva_calc > 0:
            champs_valides["montant_tva"] = str(tva_calc)


def _nettoyer_montant_ocr(valeur_str: str) -> str:
    s = str(valeur_str).strip()
    s = re.sub(r'\s*(?:DT|TND|€|\$|EUR)\s*$', '', s, flags=re.IGNORECASE).strip()
    return s


# ──────────────────────────────────────────────────────────────────────
# PIPELINE DÉDIÉ — PAYMENT ENTRY (Chèque & Traite)
# ──────────────────────────────────────────────────────────────────────

def _executer_pipeline_payment_entry(chemin_tmp, texte_brut, score,
                                      payment_method, nom_fichier,
                                      uploaded_by, job_token):
    from ocr_intelligent.ocr.payment_doc_extractor import analyser_document_paiement

    try:
        analyse = analyser_document_paiement(
            chemin_img=chemin_tmp,
            texte_ocr=texte_brut,
            payment_method=payment_method,
            score_ocr=score,
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Payment Entry Pipeline")
        _stocker_erreur(job_token, "Erreur analyse document paiement : {}".format(str(e)))
        return

    if not analyse.get("valid"):
        erreurs = analyse.get("errors") or []
        message = erreurs[0] if erreurs else "Document non valide pour ce mode de paiement."
        _stocker_resultat(job_token, {
            "success":                False,
            "type_document":          analyse.get("document_type_detected", "inconnu"),
            "titre":                  "Document non traitable",
            "erreur":                 message,
            "errors":                 erreurs,
            "image_enhanced":         analyse.get("image_enhanced", False),
            "ocr_payment_validation": analyse,
        })
        return

    champs_remplis = analyse.get("champs_remplis") or {}
    form_fields    = analyse.get("form_fields")    or {}
    type_detecte   = analyse.get("document_type_detected", "inconnu")

    if not champs_remplis and not form_fields:
        _stocker_resultat(job_token, {
            "success":       False,
            "type_document": type_detecte,
            "erreur": (
                "Document analysé (type : {}) "
                "mais aucun champ n'a pu être extrait. "
                "Vérifiez la qualité de l'image.".format(type_detecte)
            ),
        })
        return

    # Créer / mettre à jour le OCR Document pour que field_extractor puisse le relire
    tous_champs  = {**form_fields, **champs_remplis}
    ocr_doc_name = None
    for variante in _variantes_nom(nom_fichier):
        existants = frappe.get_list(
            "OCR Document",
            filters={"document_name": variante},
            fields=["name"],
            limit=1,
        )
        if existants:
            ocr_doc_name = existants[0]["name"]
            break

    statut = "Validé" if champs_remplis else "Validation requise"
    if ocr_doc_name:
        frappe.db.set_value("OCR Document", ocr_doc_name, {
            "extracted_field":  json.dumps(tous_champs, ensure_ascii=False, indent=2),
            "confidence_score": score,
            "status":           statut,
        })
    else:
        ocr_doc_obj = frappe.get_doc({
            "doctype":          "OCR Document",
            "document_name":    nom_fichier,
            "uploaded_by":      uploaded_by,
            "confidence_score": score,
            "extracted_text":   texte_brut,
            "extracted_field":  json.dumps(tous_champs, ensure_ascii=False, indent=2),
            "status":           statut,
        })
        ocr_doc_obj.insert(ignore_permissions=True)
        ocr_doc_name = ocr_doc_obj.name

    frappe.db.commit()

    _stocker_resultat(job_token, {
        "success":             True,
        "type_document":       type_detecte,
        "champs_remplis":      champs_remplis,
        "form_fields":         form_fields,
        "uncertain_fields":    analyse.get("uncertain_fields", []),
        "image_enhanced":      analyse.get("image_enhanced", False),
        "date_cheque_retenue": analyse.get("date_cheque_retenue"),
        "score_confiance":     score,
        "nom_fichier":         nom_fichier,
        "ocr_document_id":     ocr_doc_name,
        "message": "{} champ(s) extrait(s) depuis le document {} (score OCR : {}%)".format(
            len(champs_remplis), type_detecte, score
        ),
    })


def _stocker_resultat(job_token, result):
    result_json = json.dumps(result, ensure_ascii=False)
    frappe.cache().set_value(
        "ocr_pipeline_result_{}".format(job_token),
        result_json,
        expires_in_sec=3600
    )
    frappe.cache().set_value(
        "ocr_pipeline_status_{}".format(job_token),
        "termine",
        expires_in_sec=3600
    )


def _stocker_erreur(job_token, message):
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    message_json = json.dumps(message, ensure_ascii=False)
    frappe.cache().set_value(
        "ocr_pipeline_erreur_{}".format(job_token),
        message_json,
        expires_in_sec=3600
    )
    frappe.cache().set_value(
        "ocr_pipeline_status_{}".format(job_token),
        "erreur",
        expires_in_sec=3600
    )


# ──────────────────────────────────────────────────────────────────────
# PASSE OCR DÉDIÉE À L'EN-TÊTE
# ──────────────────────────────────────────────────────────────────────

def _extraire_champs_entete_image(chemin_fichier):
    try:
        from ocr_intelligent.ocr.header_extractor import extraire_champs_entete

        ext = os.path.splitext(chemin_fichier)[1].lower()
        if ext == ".pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(chemin_fichier, dpi=300)
            if not pages:
                return {}
            pil_full = pages[0].convert("RGB")
        else:
            pil_full = PILImage.open(chemin_fichier).convert("RGB")

        w, h = pil_full.size

        pil_big = pil_full.resize((w * 3, h * 3), PILImage.LANCZOS)
        texte_raw = pytesseract.image_to_string(
            pil_big, lang="fra+eng", config="--oem 3 --psm 6"
        )

        img_cv = cv2.cvtColor(np.array(pil_full), cv2.COLOR_RGB2BGR)
        zone   = img_cv[:int(h * 0.50), :]
        gris   = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY) if len(zone.shape) == 3 else zone
        hh, ww = gris.shape[:2]
        if ww < 2500:
            scale = 2500 / ww
            gris  = cv2.resize(gris, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        binaire    = cv2.adaptiveThreshold(
            gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
        )
        horizontal = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((1, 40), np.uint8))
        vertical   = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((40, 1), np.uint8))
        grille     = cv2.add(horizontal, vertical)
        propre     = cv2.add(binaire, grille)
        pil_propre = PILImage.fromarray(propre)
        texte_clean = pytesseract.image_to_string(
            pil_propre, lang="fra+eng", config="--oem 3 --psm 6"
        )

        texte_combine = texte_raw + "\n" + texte_clean
        resultat      = extraire_champs_entete(texte_combine)

        champs = {}
        for champ, valeur in resultat.get("champs", {}).items():
            if valeur is not None and str(valeur).strip():
                champs[champ] = valeur

        if "numero_facture" in champs:
            num_val = champs["numero_facture"]
            if " " in num_val and re.search(r'[A-Za-z]{3,}', num_val.split(" ", 1)[1] or ""):
                m_num = re.match(r'^([A-Za-z0-9\-/]+)', num_val)
                if m_num:
                    champs["numero_facture"] = m_num.group(1)
                else:
                    del champs["numero_facture"]
            elif len(num_val) > 30:
                del champs["numero_facture"]

        if "date" not in champs:
            date = _extraire_date_heuristique(texte_combine)
            if date:
                champs["date"] = date

        if "date" not in champs:
            date = _extraire_date_crop(pil_full)
            if date:
                champs["date"] = date

        if "fournisseur" not in champs:
            fournisseur = _detecter_fournisseur_heuristique(texte_raw)
            if fournisseur:
                champs["fournisseur"] = fournisseur

        if "numero_facture" not in champs:
            num = _extraire_numero_facture_heuristique(texte_combine)
            if num:
                champs["numero_facture"] = num

        return champs

    except Exception as e:
        try:
            frappe.log_error("Extraction en-tête image: {}".format(e), "OCR Header Image")
        except Exception:
            pass
        return {}


def _extraire_date_heuristique(texte):
    matches = re.findall(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})', texte)
    for d, m, y in matches:
        d_int, m_int = int(d), int(m)
        y_int = int(y)
        if y_int < 100:
            y_int += 2000
        if 1 <= d_int <= 31 and 1 <= m_int <= 12 and 2000 <= y_int <= 2050:
            return "{}/{}/{}".format(d, m, y)
    return None


def _extraire_date_crop(pil_img):
    try:
        w, h = pil_img.size
        for start_pct, end_pct in [(0.08, 0.17), (0.10, 0.20), (0.06, 0.22)]:
            y0, y1 = int(h * start_pct), int(h * end_pct)
            crop   = pil_img.crop((0, y0, w, y1))
            cw, ch = crop.size
            big    = crop.resize((cw * 8, ch * 8), PILImage.LANCZOS)
            cv     = cv2.cvtColor(np.array(big), cv2.COLOR_RGB2GRAY)
            _, cv  = cv2.threshold(cv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            pil_cv = PILImage.fromarray(cv)
            texte  = pytesseract.image_to_string(
                pil_cv, lang="fra+eng", config="--oem 3 --psm 6"
            )
            date = _extraire_date_heuristique(texte)
            if date:
                return date
    except Exception:
        pass
    return None


def _extraire_numero_facture_heuristique(texte):
    patterns = [
        r"\b((?:FAC|FACT|INV|FC|FACTURE|BILL)[/\-]?\d{2,4}[/\-]?\d{0,6})\b",
        r"(?:num[eé]ro\s*(?:de\s*)?facture|n[°o]\s*(?:de\s*)?facture)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,25})",
        r"(?:facture)\s*n[°o]\s*[:\-]?\s*(\d{1,10})",
        r"\bN[°o]\s*[:\-]?\s*(\d{1,10})",
        r"\bFACT[-\s]?(\d{2,})\b",
    ]
    for pat in patterns:
        m = re.search(pat, texte, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if re.fullmatch(r'\d{4}', val):
                continue
            if val and len(val) <= 30:
                return val
    return None


def _detecter_fournisseur_heuristique(texte):
    try:
        fournisseurs_connus = frappe.get_list(
            "Supplier", fields=["name", "supplier_name"], limit=200
        )
        texte_lower = texte.lower()

        for f in fournisseurs_connus:
            nom = (f.get("supplier_name") or f.get("name") or "").strip()
            if len(nom) >= 3 and nom.lower() in texte_lower:
                return nom

        try:
            from rapidfuzz import fuzz
            lignes_texte = [l.strip() for l in texte.splitlines()[:30] if l.strip()]
            for f in fournisseurs_connus:
                nom = (f.get("supplier_name") or f.get("name") or "").strip()
                if len(nom) < 3:
                    continue
                for ligne in lignes_texte:
                    score = fuzz.partial_ratio(nom.lower(), ligne.lower())
                    if score >= 80:
                        return nom
        except ImportError:
            pass
    except Exception:
        pass

    _PAT_FORME_JUR = re.compile(r'\b(?:SARL|SA\b|SUARL|SAS\b|EURL|SNC|GIE)\b')
    for ligne in texte.splitlines()[:20]:
        ligne_s = ligne.strip()
        if not ligne_s or len(ligne_s) < 3:
            continue
        m_forme = _PAT_FORME_JUR.search(ligne_s)
        if m_forme:
            nom = re.sub(r'\s+', ' ', ligne_s[:m_forme.end()]).strip()
            if len(nom) >= 3:
                return nom

    _LIGNES_TABLEAU = re.compile(
        r'\b(?:d[eé]signation|description|article|qte|qt[eé]|prix|total|tva|montant|designation)\b',
        re.IGNORECASE
    )
    _LIGNES_SKIP = re.compile(
        r'(?:rue|avenue|blvd|boulevard|tel|tél|fax|mail|@|mf\s*:|matricule|r\.c\.s|siret|rib|iban|page)'
        r'|\d{8,}',
        re.IGNORECASE
    )
    _MOTS_ARTICLE = re.compile(
        r'\b(?:SOURIS|CLAVIER|ECRAN|IMPRIMANTE|CHARGEUR|CARTES|MEMOIRES|BOITIER|CABLES?|CABLES?)\b',
        re.IGNORECASE
    )
    lignes_brutes  = texte.splitlines()
    nb_lignes_ok   = 0
    for ligne in lignes_brutes:
        ligne_s = ligne.strip()
        if not ligne_s:
            continue
        if _LIGNES_TABLEAU.search(ligne_s):
            break
        nb_lignes_ok += 1
        if nb_lignes_ok > 10:
            break
        if _LIGNES_SKIP.search(ligne_s):
            continue
        if _MOTS_ARTICLE.search(ligne_s):
            continue
        if len(ligne_s) < 3 or re.match(r'^[\d\s\-._|/]+$', ligne_s):
            continue
        lettres = re.sub(r'[^A-Za-zÀ-ÿ0-9\-]', '', ligne_s)
        if len(lettres) < 2:
            continue
        ligne_propre = re.sub(r'^[^A-Za-zÀ-ÿ]+', '', ligne_s).strip()
        if not ligne_propre or len(ligne_propre) < 2:
            continue
        if re.match(
            r'^(facture|invoice|devis|bon\s*de|n°|date|client|fournisseur)$',
            ligne_propre.lower()
        ):
            continue
        return ligne_propre[:80]

    idx_tableau = len(lignes_brutes)
    for i, l in enumerate(lignes_brutes[:12]):
        if _LIGNES_TABLEAU.search(l):
            idx_tableau = i
            break

    for ligne in lignes_brutes[:idx_tableau]:
        ligne_s = ligne.strip()
        if not ligne_s or len(ligne_s) < 3:
            continue
        mots_maj  = re.findall(r'\b[A-ZÀ-Ü]{2,}\b', ligne_s)
        texte_maj = " ".join(mots_maj)
        if len(texte_maj) >= 3 and not re.search(
            r'\b(?:FACTURE|INVOICE|DATE|TVA|TOTAL|MONTANT|DESIGNATION|QTE|PRIX|CLIENT|ADRESSE|TEL|FAX|PAGE|TIMBRE|FODEC)\b',
            texte_maj
        ) and not _MOTS_ARTICLE.search(texte_maj):
            return texte_maj

    return None


# ──────────────────────────────────────────────────────────────────────
# PASSE OCR DÉDIÉE AUX MONTANTS — v5
# ──────────────────────────────────────────────────────────────────────

def _extraire_montants_image(chemin_fichier):
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
                pil   = PILImage.open(chemin_fichier).convert("RGB")
                img_cv = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

        h, w = img_cv.shape[:2]

        zone = img_cv[int(h * 0.55):, :]
        gris = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY) if len(zone.shape) == 3 else zone

        hh, ww = gris.shape[:2]
        if ww < 2500:
            scale = 2500 / ww
            gris  = cv2.resize(gris, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        binaire = cv2.adaptiveThreshold(
            gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
        )
        horizontal = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((1, 40), np.uint8))
        vertical   = cv2.morphologyEx(binaire, cv2.MORPH_OPEN, np.ones((40, 1), np.uint8))
        grille     = cv2.add(horizontal, vertical)
        propre     = cv2.add(binaire, grille)

        pil_img = PILImage.fromarray(propre)

        texte_sparse = pytesseract.image_to_string(
            pil_img, lang="fra+eng", config="--oem 3 --psm 11"
        )
        texte_col = pytesseract.image_to_string(
            pil_img, lang="fra+eng", config="--oem 3 --psm 4"
        )

        r_sparse  = _parser_montants(texte_sparse)
        r_col     = _parser_montants(texte_col)
        resultats = _fusionner_montants(r_sparse, r_col)

        return resultats

    except Exception as e:
        try:
            frappe.log_error("Extraction montants image: {}".format(e), "OCR Montants")
        except Exception:
            pass
        return {}


def _parser_montants(texte):
    """
    Parse le texte OCR de la zone totaux.

    CORRECTIONS v5 :
    - Dédoublonnage correct avant sommation : round(v, 3) AVANT set()
      → évite les faux doublons flottants (ex: 15.209999 vs 15.21).
    - Recalcul HT FINAL garanti après agrégation TVA multi-taux.

    CORRECTIONS v3/v4 (conservées) :
    - Collecte TVA multi-taux élargie : capture toutes les lignes contenant
      un pattern XX% (avec ou sans le mot "tva"), pour agréger les TVA multi-taux.
    - Les valeurs de taux numériques (ex: 5.5, 20.0) sont exclues des montants.
    - Second recalcul HT ajouté après l'agrégation multi-taux.
    """
    resultats = {}
    lignes    = texte.splitlines()

    label_patterns_ordered = [
        # ── Priorité HAUTE : labels tunisiens standard ──────────────
        ("montant_ht",   r"(?:montant\s*h\.?t\.?)"),
        ("montant_ttc",  r"(?:montant\s*t\.?t\.?c\.?)"),
        ("montant_tva",  r"(?:montant\s*tva|montant\s*t\.?v\.?a\.?)"),
        # ── Labels génériques ────────────────────────────────────────
        ("_base_tva",    r"(?:base\s*tva|baso\s*tva)"),
        ("montant_ttc",  r"(?:t\.?t\.?c|net\s*[àa]\s*payer|total\s*g[ée]n(?:[eé]ral)?|leite|amount\s*due|\bttc\b|total\s*fac)"),
        ("montant_ht",   r"(?:total\s*h\.?t|hors\s*taxe|sous.?total|net\s*ht|\btear\b|\bh\.?t\.?\b|base\s*imposable|net\s*commercial)"),
        ("_fodec",       r"(?:fodec|fodeg)"),
        ("_timbre",      r"(?:timbre|timbr)"),
        ("montant_tva",  r"(?:\btva\b|t\.v\.a|total\s*tax|tottex|taxe\s*sur\s*valeur|tva\s*\d+\s*%?)"),
    ]

    MONTANT_MIN = 0.5
    MONTANT_MAX = 999_999

    def _est_montant_valide(n):
        return n is not None and MONTANT_MIN < n < MONTANT_MAX

    def _est_ligne_footer(ligne):
        ll = ligne.lower()
        if re.search(r'\d{10,}', re.sub(r'\s', '', ll)):
            return True
        if re.search(r'tva\s*:\s*\d{5,}', ll):
            return True
        if re.search(r't[ée]l|fax|adress|mail|@|www\.|\.com|\.tn', ll):
            return True
        if re.search(r'au\s+capital|sarl\s+au|capital\s+de|r\.i\.b|rib\s*:', ll):
            return True
        if re.search(r'scann|sign|cachet|facture\s+[àa]\s+la\s+somm', ll):
            return True
        if re.fullmatch(r'[\d\s\-]{15,}', ligne.strip()):
            return True
        return False

    # ── Collecte TVA multi-taux élargie (v3) ──────────────────────────
    # Capture les lignes contenant "tva" OU un taux "XX%"
    PAT_LIGNE_TVA = re.compile(
        r'(?:tva|t\.v\.a\.?|vat|taxe)\b|\b\d{1,2}(?:[.,]\d{1,2})?\s*%',
        re.IGNORECASE
    )
    tva_details_candidats = []

    for i, ligne in enumerate(lignes):
        ll = ligne.lower().strip()
        if not ll or _est_ligne_footer(ligne):
            continue

        if PAT_LIGNE_TVA.search(ll):
            nums_ligne = [n for n in _extraire_tous_nombres_tunisiens(ligne) if _est_montant_valide(n)]

            # Identifier et exclure les valeurs de taux (ex: 5.5, 20.0)
            taux_trouves = re.findall(r'(\d{1,2}(?:[.,]\d{1,2})?)\s*%', ligne)
            valeurs_taux = set()
            for t in taux_trouves:
                try:
                    valeurs_taux.add(float(t.replace(",", ".")))
                except Exception:
                    pass

            montants_tva_ligne = [
                n for n in nums_ligne
                if n not in valeurs_taux and n > 1.0
            ]
            tva_details_candidats.extend(montants_tva_ligne)

    # Phase 1 : label + nombre sur la même ligne
    for i, ligne in enumerate(lignes):
        ll = ligne.lower().strip()
        if not ll or _est_ligne_footer(ligne):
            continue

        for champ, pat in label_patterns_ordered:
            if champ in resultats:
                continue
            if re.search(pat, ll, re.IGNORECASE):
                nombre = _extraire_nombre_tunisien(ligne)
                if _est_montant_valide(nombre):
                    resultats[champ] = nombre
                break

    # Phase 2 : label sans nombre → chercher dans les lignes voisines
    for i, ligne in enumerate(lignes):
        ll = ligne.lower().strip()
        if not ll or _est_ligne_footer(ligne):
            continue
        for champ, pat in label_patterns_ordered:
            if champ in resultats:
                continue
            if re.search(pat, ll, re.IGNORECASE):
                for delta in [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]:
                    j = i + delta
                    if 0 <= j < len(lignes) and not _est_ligne_footer(lignes[j]):
                        nombre = _extraire_nombre_tunisien(lignes[j])
                        if _est_montant_valide(nombre):
                            resultats[champ] = nombre
                            break
                break

    # Phase 3 : heuristique par taille
    tous_nombres = []
    for ligne in lignes:
        if _est_ligne_footer(ligne):
            continue
        nums = _extraire_tous_nombres_tunisiens(ligne)
        tous_nombres.extend(n for n in nums if _est_montant_valide(n))

    tous_nombres = sorted(set(tous_nombres), reverse=True)
    gros = [n for n in tous_nombres if n > 100]

    if gros and "montant_ttc" not in resultats:
        resultats["montant_ttc"] = gros[0]

    if "montant_ht" not in resultats and gros:
        ttc_connu = resultats.get("montant_ttc", float("inf"))
        if ttc_connu != float("inf"):
            gros_ht = [n for n in gros if (n < ttc_connu * 0.99 and n >= ttc_connu * 0.40)]
        else:
            gros_ht = [n for n in gros]
        if gros_ht:
            resultats["montant_ht"] = gros_ht[0]

    if "montant_ttc" in resultats and "montant_ht" in resultats and "montant_tva" not in resultats:
        tva = round(resultats["montant_ttc"] - resultats["montant_ht"], 3)
        if tva > 0:
            resultats["montant_tva"] = tva

    # ── Agrégation TVA multi-taux (v5 — dédoublonnage corrigé) ────────
    if tva_details_candidats:
        # CORRECTION v5 : arrondir AVANT de créer le set pour éviter
        # les faux doublons liés à la précision flottante.
        # Ex: {15.209999, 15.21} → après round(v,3) → {15.21}
        tva_uniques = list({round(v, 3) for v in tva_details_candidats})
        tva_sum = round(sum(tva_uniques), 3)

        tva_current = resultats.get("montant_tva")
        if tva_current is None:
            if _est_montant_valide(tva_sum):
                resultats["montant_tva"] = tva_sum
        elif tva_current > 0 and tva_sum > tva_current * 1.10 and _est_montant_valide(tva_sum):
            ttc = resultats.get("montant_ttc")
            if ttc and tva_sum < ttc * 0.80:
                resultats["montant_tva"] = tva_sum

    # ── Recalcul HT FINAL après agrégation multi-taux (v4/v5) ─────────
    # Le premier calcul (Phase 3) peut avoir raté HT si la TVA n'était
    # pas encore agrégée. On recalcule ici avec la TVA finale.
    ttc_final = resultats.get("montant_ttc")
    tva_final = resultats.get("montant_tva")
    ht_final  = resultats.get("montant_ht")

    if ttc_final and tva_final and (not ht_final or ht_final <= 0):
        ht_calc = round(ttc_final - tva_final, 3)
        if _est_montant_valide(ht_calc):
            resultats["montant_ht"] = ht_calc

    # Compléter HT/TVA à partir de TTC quand partiellement détecté
    if "montant_ttc" in resultats and "montant_tva" in resultats and "montant_ht" not in resultats:
        ht_calc = round(resultats["montant_ttc"] - resultats["montant_tva"], 3)
        if _est_montant_valide(ht_calc):
            resultats["montant_ht"] = ht_calc

    if "montant_ttc" in resultats and "montant_ht" in resultats and "montant_tva" not in resultats:
        tva_calc = round(resultats["montant_ttc"] - resultats["montant_ht"], 3)
        if _est_montant_valide(tva_calc):
            resultats["montant_tva"] = tva_calc

    if "_fodec" in resultats and "montant_ht" not in resultats:
        ht_estime = round(resultats["_fodec"] / 0.01, 3)
        if ht_estime > 100:
            resultats["montant_ht"] = ht_estime

    resultats.pop("_fodec",    None)
    resultats.pop("_timbre",   None)
    resultats.pop("_base_tva", None)

    return resultats


def _extraire_nombre_tunisien(ligne):
    nums = _extraire_tous_nombres_tunisiens(ligne)
    return nums[-1] if nums else None


def _extraire_tous_nombres_tunisiens(ligne):
    """
    Extrait tous les nombres d'une ligne (format tunisien ET international).

    CORRECTIONS v3 (conservées) :
    - Détection PRÉALABLE du format anglais "N,NNN.NN" → 1,413.22 → 1413.22
    - Dédoublonnage flottant (tolérance 0.001)
    - Filtre séquences > 6 chiffres sans décimale (RIB/compte)
    """
    resultats = []

    # ── Étape 1 : format anglais "N,NNN.NN" ─────────────────────────
    pat_anglais = re.findall(r'\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\b', ligne)
    for m in pat_anglais:
        parties = m.split(",")
        parties_entieres = [p.split(".")[0] for p in parties[1:]]
        if all(len(p) == 3 and p.isdigit() for p in parties_entieres):
            valeur_str = m.replace(",", "")
            try:
                v = float(valeur_str)
                if 0.5 < v < 999_999:
                    resultats.append(v)
            except ValueError:
                pass

    # ── Étape 2 : parsing tunisien (espace comme séparateur milliers) ─
    matches = re.findall(r'(\d[\d\s.]*(?:,\d{1,3})?)', ligne)
    for m in matches:
        s = m.strip().rstrip(".")
        if not s or not any(c.isdigit() for c in s):
            continue

        if "," in s:
            avant_virgule, apres_virgule = s.split(",", 1)
        else:
            avant_virgule, apres_virgule = s, None

        groupes = avant_virgule.strip().split()
        if len(groupes) > 1:
            def _partie_entiere(g):
                return g.split(".", 1)[0] if "." in g else g.replace(".", "")

            valide = all(re.fullmatch(r'\d{3}', _partie_entiere(g)) for g in groupes[1:])
            if not valide:
                avant_virgule = groupes[-1]
            else:
                last_g = groupes[-1]
                if "." in last_g and apres_virgule is None:
                    int_parts = [g.split(".", 1)[0] for g in groupes]
                    avant_virgule = "".join(int_parts)
                    apres_virgule = last_g.split(".", 1)[1]
                else:
                    avant_virgule = "".join(g.replace(" ", "") for g in groupes)

        avant_virgule = re.sub(r'\s+', '', avant_virgule)

        if "." in avant_virgule:
            parties = avant_virgule.split(".")
            if len(parties) > 2:
                avant_virgule = "".join(parties)
            elif len(parties) == 2 and len(parties[1]) == 3 and apres_virgule is None:
                pass
            elif len(parties) == 2 and apres_virgule is not None:
                avant_virgule = "".join(parties)

        if apres_virgule is not None:
            s = avant_virgule + "." + apres_virgule
        else:
            s = avant_virgule

        try:
            v = float(s)
            if 0.5 < v < 999_999:
                resultats.append(v)
        except ValueError:
            continue

    # ── Étape 3 : dédoublonnage avec tolérance flottante ────────────
    dedup = []
    for v in resultats:
        if not any(abs(v - x) < 0.001 for x in dedup):
            dedup.append(v)
    return dedup


def _fusionner_montants(r1, r2):
    """
    Fusionne les résultats de deux passes OCR.
    Seuil anti-RIB : ratio > 100.
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

    if "montant_ttc" in candidats:
        vals_ttc = sorted(set(candidats["montant_ttc"]))
        if len(vals_ttc) >= 2:
            smallest, largest = vals_ttc[0], vals_ttc[-1]
            if smallest > 0 and largest / smallest > 100:
                resultats["montant_ttc"] = smallest
            else:
                resultats["montant_ttc"] = largest
        else:
            resultats["montant_ttc"] = vals_ttc[0]

    if "montant_ht" in candidats:
        ttc    = resultats.get("montant_ttc", float("inf"))
        valides = [v for v in candidats["montant_ht"] if v < ttc]
        if valides:
            resultats["montant_ht"] = max(valides)
        else:
            resultats["montant_ht"] = min(candidats["montant_ht"])

    if "montant_tva" in candidats:
        ttc = resultats.get("montant_ttc")
        ht  = resultats.get("montant_ht")
        if ttc and ht:
            tva_attendue = ttc - ht
            meilleur     = min(candidats["montant_tva"], key=lambda v: abs(v - tva_attendue))
            resultats["montant_tva"] = meilleur
        else:
            resultats["montant_tva"] = candidats["montant_tva"][0]

    if "montant_tva" not in resultats and "montant_ttc" in resultats and "montant_ht" in resultats:
        tva = round(resultats["montant_ttc"] - resultats["montant_ht"], 3)
        if tva > 0:
            resultats["montant_tva"] = tva

    if "montant_ht" not in resultats and "montant_ttc" in resultats and "montant_tva" in resultats:
        ht = round(resultats["montant_ttc"] - resultats["montant_tva"], 3)
        if ht > 0:
            resultats["montant_ht"] = ht

    return resultats


# ──────────────────────────────────────────────────────────────────────
# CORRECTION EXTENSION
# ──────────────────────────────────────────────────────────────────────

def _corriger_extension(nom, ct, contenu):
    ext = os.path.splitext(nom)[1].lower()
    if ext in [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]:
        return nom, ext

    map_ct = {
        "image/jpeg":      ".jpg",
        "image/jpg":       ".jpg",
        "image/png":       ".png",
        "image/tiff":      ".tiff",
        "image/bmp":       ".bmp",
        "application/pdf": ".pdf",
    }
    ct_clean = (ct or "").split(";")[0].strip()
    if ct_clean in map_ct:
        return nom + map_ct[ct_clean], map_ct[ct_clean]

    if contenu:
        sig = contenu[:8]
        if sig[:4] == b'\x89PNG':  return nom + ".png", ".png"
        if sig[:2] == b'\xff\xd8': return nom + ".jpg", ".jpg"
        if sig[:4] == b'%PDF':     return nom + ".pdf", ".pdf"
        if sig[:2] == b'BM':       return nom + ".bmp", ".bmp"

    return nom, ext if ext else ""