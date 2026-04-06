"""
OCR Engine v4 — Groupe Bayoudh Metal
Prétraitement multi-stratégie pour images dégradées

Nouveautés v4 :
  ✔ Pipeline multi-stratégie : 5 prétraitements différents testés en parallèle
  ✔ Stratégie "rescue" pour images quasi-vides (score=95% mais 0 mot)
  ✔ Détection automatique si l'image est inversée (texte blanc sur fond noir)
  ✔ Correction de rotation 90°/180°/270° automatique
  ✔ Débruitage NL-Means (plus efficace que MedianFilter pour scans bruités)
  ✔ Sélection du meilleur résultat parmi toutes les stratégies × tous les PSM
"""

import cv2
import numpy as np
import pytesseract
from PIL import Image as PILImage, ImageFilter, ImageEnhance, ImageOps
from pdf2image import convert_from_path
import os


class OCREngine:

    PSM_MODES = [
        ("--oem 3 --psm 11", "sparse_text"),
        ("--oem 3 --psm 4",  "single_column"),
        ("--oem 3 --psm 3",  "auto"),
        ("--oem 3 --psm 6",  "uniform_block"),
    ]

    LANGUES = "fra+ara+eng"

    def __init__(self):
        pass

    # ─────────────────────────────────────────────────────────────
    # API PUBLIQUE
    # ─────────────────────────────────────────────────────────────

    def extraire_texte(self, chemin_fichier):
        ext = os.path.splitext(chemin_fichier)[1].lower()
        if ext == ".pdf":
            return self._traiter_pdf(chemin_fichier)
        elif ext in [".png", ".jpg", ".jpeg", ".tiff", ".bmp"]:
            return self._traiter_image(chemin_fichier)
        else:
            raise ValueError(f"Format non supporté : {ext}")

    # ─────────────────────────────────────────────────────────────
    # TRAITEMENT IMAGE — MULTI-STRATÉGIE
    # ─────────────────────────────────────────────────────────────

    def _traiter_image(self, chemin):
        # Lecture robuste
        img_cv = self._lire_image_robuste(chemin)
        if img_cv is None:
            raise ValueError(f"Impossible de lire l'image : {chemin}")

        # Corriger la rotation avant tout
        img_cv = self._corriger_rotation(img_cv)

        # Générer toutes les variantes prétraitées
        variantes = self._generer_variantes(img_cv)

        # Tester toutes les variantes × tous les PSM → garder le meilleur
        meilleur_texte = ""
        meilleur_score = 0
        meilleur_mots  = 0
        meilleur_psm   = "—"
        meilleur_strat = "—"
        debug_info     = {}

        for nom_strat, pil_img in variantes:
            texte, score, psm = self._ocr_multi_psm(pil_img)
            mots = len([m for m in texte.split() if len(m) > 1])
            critere = score * 0.5 + min(mots * 3, 50) * 0.5

            if critere > (meilleur_score * 0.5 + min(meilleur_mots * 3, 50) * 0.5):
                meilleur_texte = texte
                meilleur_score = score
                meilleur_mots  = mots
                meilleur_psm   = psm
                meilleur_strat = nom_strat

        debug_info["strategie_choisie"] = meilleur_strat
        debug_info["nb_mots_extraits"]  = meilleur_mots
        debug_info["nb_variantes_testees"] = len(variantes)

        return {
            "texte":            meilleur_texte,
            "score_confiance":  meilleur_score,
            "nombre_pages":     1,
            "methode":          f"multi_strategie_v4/{meilleur_strat}",
            "psm_selectionne":  meilleur_psm,
            "debug":            debug_info,
        }

    # ─────────────────────────────────────────────────────────────
    # TRAITEMENT PDF
    # ─────────────────────────────────────────────────────────────

    def _traiter_pdf(self, chemin):
        pages = convert_from_path(chemin, dpi=300)
        textes, scores, psms = [], [], []

        for page in pages:
            img_cv = np.array(page)
            img_cv = self._corriger_rotation(img_cv)
            variantes = self._generer_variantes(img_cv)

            meilleur_texte, meilleur_score, meilleur_psm = "", 0, "—"
            for _, pil_img in variantes:
                t, s, psm = self._ocr_multi_psm(pil_img)
                mots = len([m for m in t.split() if len(m) > 1])
                if s * 0.5 + min(mots * 3, 50) * 0.5 > meilleur_score:
                    meilleur_texte, meilleur_score, meilleur_psm = t, s, psm

            textes.append(meilleur_texte)
            scores.append(meilleur_score)
            psms.append(meilleur_psm)

        return {
            "texte":            "\n\n".join(textes).strip(),
            "score_confiance":  round(sum(scores) / len(scores), 1) if scores else 0,
            "nombre_pages":     len(pages),
            "methode":          "pdf_multi_strategie_v4",
            "psm_selectionne":  psms[0] if psms else "—",
            "debug":            {},
        }

    # ─────────────────────────────────────────────────────────────
    # LECTURE ROBUSTE
    # ─────────────────────────────────────────────────────────────

    def _lire_image_robuste(self, chemin):
        """Essaie plusieurs méthodes de lecture pour maximiser la compatibilité."""
        # Méthode 1 : OpenCV direct
        img = cv2.imread(chemin)
        if img is not None:
            return img

        # Méthode 2 : via PIL (gère mieux certains PNG/TIFF)
        try:
            pil = PILImage.open(chemin).convert("RGB")
            return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        except Exception:
            pass

        # Méthode 3 : lecture binaire + décodage numpy
        try:
            with open(chemin, "rb") as f:
                data = np.frombuffer(f.read(), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            return img
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # CORRECTION ROTATION (90° / 180° / 270°)
    # ─────────────────────────────────────────────────────────────

    def _corriger_rotation(self, img_cv):
        """
        Détecte et corrige une rotation de 90°, 180° ou 270°.
        Utilise la densité de texte pour chaque orientation.
        """
        try:
            gris = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY) if len(img_cv.shape) == 3 else img_cv

            rotations = {
                0:   img_cv,
                90:  cv2.rotate(img_cv, cv2.ROTATE_90_CLOCKWISE),
                180: cv2.rotate(img_cv, cv2.ROTATE_180),
                270: cv2.rotate(img_cv, cv2.ROTATE_90_COUNTERCLOCKWISE),
            }

            meilleur_angle  = 0
            meilleur_score  = -1

            for angle, img_rot in rotations.items():
                gris_rot = cv2.cvtColor(img_rot, cv2.COLOR_BGR2GRAY) if len(img_rot.shape) == 3 else img_rot
                # Binarisation rapide
                _, bin_img = cv2.threshold(gris_rot, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                # Score = projection horizontale (texte horizontal = lignes denses)
                proj_h = np.sum(bin_img, axis=1).astype(float)
                variance = float(np.var(proj_h))
                if variance > meilleur_score:
                    meilleur_score = variance
                    meilleur_angle = angle

            return rotations[meilleur_angle]

        except Exception:
            return img_cv

    # ─────────────────────────────────────────────────────────────
    # GÉNÉRATION DES VARIANTES DE PRÉTRAITEMENT
    # ─────────────────────────────────────────────────────────────

    def _generer_variantes(self, img_cv):
        """
        Génère 5 variantes prétraitées de l'image.
        Chaque variante cible un type de dégradation différent.
        """
        variantes = []

        gris = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY) if len(img_cv.shape) == 3 else img_cv.copy()

        # ── Stratégie 1 : Pipeline standard (v3) ─────────────────────
        try:
            v1, _ = self._pretraiter_standard(gris.copy())
            variantes.append(("standard", v1))
        except Exception:
            pass

        # ── Stratégie 2 : Débruitage agressif NL-Means ───────────────
        try:
            v2 = self._pretraiter_debruitage(gris.copy())
            variantes.append(("debruitage_nlmeans", v2))
        except Exception:
            pass

        # ── Stratégie 3 : Rehaussement contraste extrême ─────────────
        try:
            v3 = self._pretraiter_contraste_extreme(gris.copy())
            variantes.append(("contraste_extreme", v3))
        except Exception:
            pass

        # ── Stratégie 4 : Image inversée (texte blanc sur fond noir) ─
        try:
            v4 = self._pretraiter_inverse(gris.copy())
            variantes.append(("inverse", v4))
        except Exception:
            pass

        # ── Stratégie 5 : Rescue — image brute PIL sans binarisation ─
        try:
            v5 = self._pretraiter_rescue(gris.copy())
            variantes.append(("rescue_brut", v5))
        except Exception:
            pass

        # Fallback : image PIL brute si tout échoue
        if not variantes:
            pil_brut = PILImage.fromarray(gris)
            variantes.append(("fallback_brut", pil_brut))

        return variantes

    # ─────────────────────────────────────────────────────────────
    # STRATÉGIE 1 : STANDARD (repris de v3)
    # ─────────────────────────────────────────────────────────────

    def _pretraiter_standard(self, gris):
        debug = {}

        # Deskew
        gris, angle = self._deskew(gris)
        debug["angle"] = f"{angle:.2f}°"

        # Upscale vers 2400px
        h, w = gris.shape
        if w < 2400:
            scale = 2400 / w
            gris = cv2.resize(gris, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)

        # Correction gamma
        mean_val = float(np.mean(gris))
        gris, gamma = self._corriger_gamma(gris, mean_val)
        debug["gamma"] = gamma

        # Suppression lignes horizontales
        gris = self._supprimer_lignes_horizontales(gris)

        # Pipeline PIL
        pil = PILImage.fromarray(gris)
        pil = pil.filter(ImageFilter.MedianFilter(size=3))
        factor = 2.5 if float(np.mean(gris)) < 180 else 2.0
        pil = ImageEnhance.Contrast(pil).enhance(factor)
        pil = ImageEnhance.Sharpness(pil).enhance(3.0)
        pil = ImageOps.autocontrast(pil, cutoff=2)

        # Binarisation
        arr = np.array(pil)
        _, binaire = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        white_ratio = np.sum(binaire == 255) / binaire.size
        if white_ratio < 0.3:
            binaire = cv2.adaptiveThreshold(
                arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, blockSize=25, C=10
            )

        return PILImage.fromarray(binaire), debug

    # ─────────────────────────────────────────────────────────────
    # STRATÉGIE 2 : DÉBRUITAGE NL-MEANS
    # ─────────────────────────────────────────────────────────────

    def _pretraiter_debruitage(self, gris):
        """Débruitage Non-Local Means — très efficace sur scans bruités."""
        # Upscale
        h, w = gris.shape
        if w < 2400:
            gris = cv2.resize(gris, None, fx=2400/w, fy=2400/w, interpolation=cv2.INTER_LANCZOS4)

        # NL-Means denoising
        gris = cv2.fastNlMeansDenoising(gris, h=10, templateWindowSize=7, searchWindowSize=21)

        # CLAHE pour contraste
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gris = clahe.apply(gris)

        # Binarisation adaptative
        binaire = cv2.adaptiveThreshold(
            gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, blockSize=15, C=8
        )
        return PILImage.fromarray(binaire)

    # ─────────────────────────────────────────────────────────────
    # STRATÉGIE 3 : CONTRASTE EXTRÊME
    # ─────────────────────────────────────────────────────────────

    def _pretraiter_contraste_extreme(self, gris):
        """Pour images très pâles ou très sombres."""
        # Upscale
        h, w = gris.shape
        if w < 2400:
            gris = cv2.resize(gris, None, fx=2400/w, fy=2400/w, interpolation=cv2.INTER_LANCZOS4)

        # Étirement histogramme
        p2, p98 = np.percentile(gris, 2), np.percentile(gris, 98)
        if p98 > p2:
            gris = np.clip((gris.astype(float) - p2) / (p98 - p2) * 255, 0, 255).astype(np.uint8)

        # Morphologie pour renforcer le texte
        kernel = np.ones((2, 2), np.uint8)
        gris = cv2.morphologyEx(gris, cv2.MORPH_CLOSE, kernel)

        # Binarisation Otsu
        _, binaire = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return PILImage.fromarray(binaire)

    # ─────────────────────────────────────────────────────────────
    # STRATÉGIE 4 : IMAGE INVERSÉE
    # ─────────────────────────────────────────────────────────────

    def _pretraiter_inverse(self, gris):
        """Pour les images avec texte clair sur fond sombre."""
        # Upscale
        h, w = gris.shape
        if w < 2400:
            gris = cv2.resize(gris, None, fx=2400/w, fy=2400/w, interpolation=cv2.INTER_LANCZOS4)

        # Vérifier si l'image est majoritairement sombre
        mean_val = float(np.mean(gris))
        if mean_val > 128:
            # Image claire → inverser
            gris = cv2.bitwise_not(gris)

        _, binaire = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return PILImage.fromarray(binaire)

    # ─────────────────────────────────────────────────────────────
    # STRATÉGIE 5 : RESCUE (aucune binarisation)
    # ─────────────────────────────────────────────────────────────

    def _pretraiter_rescue(self, gris):
        """
        Stratégie de secours : upscale + contraste PIL sans binarisation.
        Parfois Tesseract fonctionne mieux sur une image en niveaux de gris.
        """
        h, w = gris.shape
        # Upscale x3
        gris = cv2.resize(gris, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_LANCZOS4)

        pil = PILImage.fromarray(gris)
        pil = ImageOps.autocontrast(pil, cutoff=1)
        pil = ImageEnhance.Contrast(pil).enhance(3.0)
        pil = ImageEnhance.Sharpness(pil).enhance(4.0)
        pil = pil.filter(ImageFilter.SHARPEN)
        return pil

    # ─────────────────────────────────────────────────────────────
    # DESKEW
    # ─────────────────────────────────────────────────────────────

    def _deskew(self, gris):
        try:
            _, seuil = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            coords   = np.column_stack(np.where(seuil > 0))
            if len(coords) < 100:
                return gris, 0.0
            angle = cv2.minAreaRect(coords)[-1]
            angle = -(90 + angle) if angle < -45 else -angle
            if abs(angle) < 0.5 or abs(angle) > 30:
                return gris, angle
            h, w = gris.shape
            M    = cv2.getRotationMatrix2D((w//2, h//2), angle, 1.0)
            corr = cv2.warpAffine(gris, M, (w, h),
                                  flags=cv2.INTER_LANCZOS4,
                                  borderMode=cv2.BORDER_REPLICATE)
            return corr, angle
        except Exception:
            return gris, 0.0

    # ─────────────────────────────────────────────────────────────
    # CORRECTION GAMMA
    # ─────────────────────────────────────────────────────────────

    def _corriger_gamma(self, gris, mean_val):
        if mean_val < 80:    gamma = 1.8
        elif mean_val < 120: gamma = 1.4
        elif mean_val > 220: gamma = 0.7
        elif mean_val > 190: gamma = 0.85
        else:                return gris, 1.0

        lut = np.array([
            min(255, int(((i / 255.0) ** (1.0 / gamma)) * 255))
            for i in range(256)
        ], dtype=np.uint8)
        return cv2.LUT(gris, lut), gamma

    # ─────────────────────────────────────────────────────────────
    # SUPPRESSION LIGNES HORIZONTALES
    # ─────────────────────────────────────────────────────────────

    def _supprimer_lignes_horizontales(self, gris):
        try:
            h, w = gris.shape
            taille_kernel = max(w // 15, 30)
            kernel_horiz  = cv2.getStructuringElement(cv2.MORPH_RECT, (taille_kernel, 1))
            lignes        = cv2.morphologyEx(gris, cv2.MORPH_OPEN, kernel_horiz, iterations=2)
            _, mask       = cv2.threshold(lignes, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            mask_dilate   = cv2.dilate(mask, np.ones((3, 1), np.uint8), iterations=1)
            masque_final  = cv2.bitwise_not(mask_dilate)
            lignes_pct    = np.sum(masque_final == 0) / masque_final.size
            if lignes_pct < 0.001:
                return gris
            return cv2.inpaint(gris, masque_final, 3, cv2.INPAINT_TELEA)
        except Exception:
            return gris

    # ─────────────────────────────────────────────────────────────
    # OCR MULTI-PSM
    # ─────────────────────────────────────────────────────────────

    def _ocr_multi_psm(self, pil_bin):
        resultats = []
        for config, nom_psm in self.PSM_MODES:
            try:
                data = pytesseract.image_to_data(
                    pil_bin, lang=self.LANGUES, config=config,
                    output_type=pytesseract.Output.DICT
                )
                conf_list = [int(c) for c in data["conf"] if str(c) != "-1" and int(c) > 0]
                score = round(sum(conf_list) / len(conf_list), 1) if conf_list else 0

                texte = pytesseract.image_to_string(pil_bin, lang=self.LANGUES, config=config)
                mots  = len([m for m in texte.split() if len(m) > 1])
                critere = score * 0.6 + min(mots * 2, 40) * 0.4

                resultats.append((critere, score, mots, texte, nom_psm))
            except Exception:
                continue

        if not resultats:
            return "", 0, "—"

        meilleur = max(resultats, key=lambda x: x[0])
        _, score, _, texte, psm = meilleur
        return texte.strip(), score, psm