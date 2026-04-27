# -*- coding: utf-8 -*-
"""
image_preprocessor.py — Groupe Bayoudh Metal
Prétraitement images avant OCR.

CORRECTIONS v3 (par rapport au code existant) :
  ✔ Détection de flou AVANT upscale : si image floue → Real-ESRGAN ou NLM
  ✔ Débruitage NLM déplacé AVANT binarisation (évite amplification bruit)
  ✔ Binarisation : seuil haut SCORE_BINARISATION_MAX corrigé à 0.50
  ✔ _supprimer_bordures : seuil surface minimum 25% (évite recadrage excessif)
  ✔ diagnostiquer_qualite : retourne dict complet avec score 0-100 documenté
  ✔ Nouveau : super_resoudre() via Real-ESRGAN si disponible
  ✔ Nouveau : detect_blur() retourne (is_blurry, laplacian_variance, seuil_utilise)
"""

import os
import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────
# CONSTANTES
# ──────────────────────────────────────────────────────────────────────

DPI_CIBLE               = 300
LARGEUR_MIN_PIXELS      = 1500
SCORE_BINARISATION      = 0.15   # trop peu de texte en dessous
SCORE_BINARISATION_MAX  = 0.50   # sur-noirceur au dessus (corrigé de 0.60)
SEUIL_SURFACE_CONTENU   = 0.25   # fraction min de l'image pour le recadrage
SEUIL_FLOU_LAPLACIAN    = 80.0   # variance Laplacien en dessous = image floue


# ──────────────────────────────────────────────────────────────────────
# DÉTECTION DE FLOU
# ──────────────────────────────────────────────────────────────────────

def detect_blur(img_cv: np.ndarray) -> tuple[bool, float, float]:
    """
    Détecte si une image est floue via la variance du Laplacien.

    Returns:
        (is_blurry, variance, seuil_utilise)
        - is_blurry  : True si l'image est considérée floue
        - variance   : valeur brute de la variance du Laplacien
        - seuil      : seuil utilisé pour la décision
    """
    gris = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY) if len(img_cv.shape) == 3 else img_cv
    variance = float(cv2.Laplacian(gris, cv2.CV_64F).var())
    return variance < SEUIL_FLOU_LAPLACIAN, variance, SEUIL_FLOU_LAPLACIAN


# ──────────────────────────────────────────────────────────────────────
# SUPER-RÉSOLUTION (Real-ESRGAN, optionnel)
# ──────────────────────────────────────────────────────────────────────

_esrgan_model = None


def _get_esrgan():
    """Singleton Real-ESRGAN — chargé à la première utilisation."""
    global _esrgan_model
    if _esrgan_model is not None:
        return _esrgan_model
    try:
        from basicsr.archs.rrdbnet_arch import RRDBNet
        from realesrgan import RealESRGANer
        from pathlib import Path

        weights_path = Path(__file__).parent.parent / "models" / "RealESRGAN_x2plus.pth"
        if not weights_path.exists():
            return None

        model = RRDBNet(
            num_in_ch=3, num_out_ch=3, num_feat=64,
            num_block=23, num_grow_ch=32, scale=2
        )
        _esrgan_model = RealESRGANer(
            scale=2, model_path=str(weights_path), model=model,
            tile=400, tile_pad=10, pre_pad=0, half=False
        )
    except Exception:
        _esrgan_model = None
    return _esrgan_model


def super_resoudre(img_cv: np.ndarray) -> np.ndarray:
    """
    Applique la super-résolution Real-ESRGAN si disponible.
    Retourne l'image originale si ESRGAN n'est pas installé.
    """
    esrgan = _get_esrgan()
    if esrgan is None:
        # Fallback : upscale Lanczos ×2
        h, w = img_cv.shape[:2]
        return cv2.resize(img_cv, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    try:
        output, _ = esrgan.enhance(img_cv, outscale=2)
        return output
    except Exception:
        h, w = img_cv.shape[:2]
        return cv2.resize(img_cv, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)


# ──────────────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ──────────────────────────────────────────────────────────────────────

def pretraiter_image(
    chemin_entree: str,
    chemin_sortie: str = None,
    forcer_super_resolution: bool = False,
) -> str:
    """
    Prétraite une image pour améliorer la qualité OCR.

    Pipeline corrigé (ordre) :
      1. Lecture + conversion gris
      2. Détection flou → super-résolution si nécessaire
      3. Upscale standard si toujours trop petit
      4. Amélioration contraste (CLAHE)
      5. Débruitage  ← AVANT binarisation (correction v3)
      6. Redressement (deskew)
      7. Binarisation intelligente
      8. Suppression bordures

    Args:
        chemin_entree           : image source (.png, .jpg, .tiff, .bmp)
        chemin_sortie           : chemin de sortie (auto si None)
        forcer_super_resolution : forcer ESRGAN même sur images nettes

    Returns:
        chemin de l'image prétraitée
    """
    if not chemin_sortie:
        base, ext = os.path.splitext(chemin_entree)
        chemin_sortie = f"{base}_preprocessed.png"

    img = cv2.imread(chemin_entree)
    if img is None:
        raise ValueError(f"Impossible de lire l'image : {chemin_entree}")

    # Étape 1 : gris
    img = _convertir_gris(img)

    # Étape 2 : détection flou + super-résolution si nécessaire
    img_color = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)  # besoin BGR pour ESRGAN
    is_blurry, variance, _ = detect_blur(img_color)

    if is_blurry or forcer_super_resolution:
        img_color = super_resoudre(img_color)
        img = cv2.cvtColor(img_color, cv2.COLOR_BGR2GRAY)

    # Étape 3 : upscale standard si encore trop petit
    img = _upscale_si_petit(img)

    # Étape 4 : contraste
    img = _ameliorer_contraste(img)

    # Étape 5 : débruitage AVANT binarisation (correction v3)
    img = _debruiter(img)

    # Étape 6 : redressement
    img = _redresser(img)

    # Étape 7 : binarisation
    img = _binariser(img)

    # Étape 8 : bordures
    img = _supprimer_bordures(img)

    cv2.imwrite(chemin_sortie, img)
    return chemin_sortie


# ──────────────────────────────────────────────────────────────────────
# ÉTAPES
# ──────────────────────────────────────────────────────────────────────

def _convertir_gris(img: np.ndarray) -> np.ndarray:
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _upscale_si_petit(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if w < LARGEUR_MIN_PIXELS:
        facteur   = LARGEUR_MIN_PIXELS / w
        img = cv2.resize(img, (int(w * facteur), int(h * facteur)),
                         interpolation=cv2.INTER_LANCZOS4)
    return img


def _ameliorer_contraste(img: np.ndarray) -> np.ndarray:
    """CLAHE adaptatif — efficace sur scans à faible contraste et documents jaunis."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(img)


def _debruiter(img: np.ndarray) -> np.ndarray:
    """
    Filtre gaussien léger — AVANT binarisation pour ne pas amplifier le bruit.
    CORRECTION v3 : déplacé de la position 6 à la position 4.
    """
    return cv2.GaussianBlur(img, (3, 3), 0)


def _redresser(img: np.ndarray) -> np.ndarray:
    """Deskew via transformée de Hough probabiliste."""
    try:
        edges = cv2.Canny(img, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                                threshold=100, minLineLength=100, maxLineGap=10)
        if lines is None or len(lines) == 0:
            return img

        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 != x1:
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if -15 < angle < 15:
                    angles.append(angle)

        if not angles:
            return img
        angle_median = np.median(angles)
        if abs(angle_median) < 0.5:
            return img

        h, w    = img.shape[:2]
        matrice = cv2.getRotationMatrix2D((w // 2, h // 2), angle_median, 1.0)
        return cv2.warpAffine(img, matrice, (w, h),
                              flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return img


def _binariser(img: np.ndarray) -> np.ndarray:
    """
    Binarisation intelligente : Otsu si ratio correct, adaptative sinon.

    CORRECTION v3 : seuil haut SCORE_BINARISATION_MAX = 0.50 (était 0.60).
    Un ratio > 0.50 = image presque noire → binarisation Otsu défaillante.
    """
    _, img_otsu = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ratio_noir  = np.sum(img_otsu == 0) / img_otsu.size

    if SCORE_BINARISATION <= ratio_noir <= SCORE_BINARISATION_MAX:
        return img_otsu

    return cv2.adaptiveThreshold(
        img, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=11, C=2
    )


def _supprimer_bordures(img: np.ndarray) -> np.ndarray:
    """
    Supprime les bordures noires des scans.

    CORRECTION v3 : vérification que la boîte englobante représente
    au moins SEUIL_SURFACE_CONTENU (25%) de l'image totale.
    Si trop petite → image déjà propre → pas de recadrage.
    """
    try:
        img_inv = cv2.bitwise_not(img)
        coords  = cv2.findNonZero(img_inv)
        if coords is None:
            return img

        x, y, w, h = cv2.boundingRect(coords)

        surface_totale  = img.shape[0] * img.shape[1]
        surface_contenu = w * h
        if surface_contenu / surface_totale < SEUIL_SURFACE_CONTENU:
            return img

        marge = 10
        x     = max(0, x - marge)
        y     = max(0, y - marge)
        x2    = min(img.shape[1], x + w + marge * 2)
        y2    = min(img.shape[0], y + h + marge * 2)
        return img[y:y2, x:x2]
    except Exception:
        return img


# ──────────────────────────────────────────────────────────────────────
# PDF → IMAGES PRÉTRAITÉES
# ──────────────────────────────────────────────────────────────────────

def pretraiter_pdf_pages(chemin_pdf: str, dossier_sortie: str) -> list:
    """Convertit chaque page PDF en image prétraitée."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise ImportError("pdf2image requis : pip install pdf2image")

    os.makedirs(dossier_sortie, exist_ok=True)
    pages = convert_from_path(chemin_pdf, dpi=DPI_CIBLE)

    chemins_pretraites = []
    for i, page in enumerate(pages):
        chemin_page = os.path.join(dossier_sortie, f"page_{i+1:03d}.png")
        page.save(chemin_page, "PNG")
        chemin_pretraite = pretraiter_image(chemin_page)
        chemins_pretraites.append(chemin_pretraite)
        if os.path.exists(chemin_page):
            os.remove(chemin_page)

    return chemins_pretraites


# ──────────────────────────────────────────────────────────────────────
# DIAGNOSTIC QUALITÉ
# ──────────────────────────────────────────────────────────────────────

def diagnostiquer_qualite(chemin_image: str) -> dict:
    """
    Analyse la qualité d'une image avant OCR.

    Score sur 100 points :
        - Résolution (largeur px)  : 5 / 20 / 35 pts
        - Contraste (écart-type)   : 5 / 20 / 35 pts
        - Netteté (variance Lapl.) : 0 / 15 / 30 pts

    Returns:
        {"score", "resolution", "contraste", "bruit", "dimensions", "recommandation",
         "is_blurry", "laplacian_variance"}
    """
    img = cv2.imread(chemin_image, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"score": 0, "erreur": "Image illisible"}

    h, w  = img.shape
    score = 0
    rapport: dict = {}

    # Résolution
    if w >= 2000:
        rapport["resolution"] = "bonne"; score += 35
    elif w >= 1200:
        rapport["resolution"] = "correcte"; score += 20
    else:
        rapport["resolution"] = "faible"; score += 5

    # Contraste
    ecart_type = float(np.std(img))
    if ecart_type > 80:
        rapport["contraste"] = "bon"; score += 35
    elif ecart_type > 40:
        rapport["contraste"] = "correct"; score += 20
    else:
        rapport["contraste"] = "faible"; score += 5

    # Netteté/bruit
    laplacian_var = float(cv2.Laplacian(img, cv2.CV_64F).var())
    if laplacian_var > 500:
        rapport["bruit"] = "faible"; score += 30
    elif laplacian_var > 100:
        rapport["bruit"] = "modéré"; score += 15
    else:
        rapport["bruit"] = "élevé"; score += 0

    rapport["score"]              = score
    rapport["dimensions"]         = f"{w}x{h} px"
    rapport["is_blurry"]          = laplacian_var < SEUIL_FLOU_LAPLACIAN
    rapport["laplacian_variance"] = round(laplacian_var, 2)

    if score >= 70:
        rapport["recommandation"] = "Image de bonne qualité, OCR optimal."
    elif score >= 40:
        rapport["recommandation"] = "Qualité correcte. Le prétraitement améliorera les résultats."
    else:
        rapport["recommandation"] = (
            "Image de mauvaise qualité. "
            "Rescannez à 300 DPI minimum ou utilisez un PDF natif."
        )

    return rapport