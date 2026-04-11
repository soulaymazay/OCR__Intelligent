"""
image_preprocessor.py — Groupe Bayoudh Metal
Prétraitement des images avant OCR pour améliorer la qualité d'extraction.

Techniques appliquées (dans l'ordre) :
  1. Conversion en niveaux de gris
  2. Débruitage (filtre gaussien léger)
  3. Redressement automatique (deskew)
  4. Binarisation adaptative (Otsu ou adaptative locale)
  5. Suppression des bordures noires
  6. Upscaling si résolution < 300 DPI estimée
  7. Amélioration du contraste (CLAHE)

Usage :
    from ocr_intelligent.ocr.image_preprocessor import pretraiter_image
    chemin_ameliore = pretraiter_image(chemin_original)
"""

import os
import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────
# CONSTANTES
# ──────────────────────────────────────────────────────────────────────

DPI_CIBLE          = 300    # DPI minimum recommandé pour l'OCR
LARGEUR_MIN_PIXELS = 1500   # En dessous → upscale
SCORE_BINARISATION = 0.15   # Ratio pixels noirs/blancs pour détecter une mauvaise binarisation


# ──────────────────────────────────────────────────────────────────────
# FONCTION PRINCIPALE
# ──────────────────────────────────────────────────────────────────────

def pretraiter_image(chemin_entree: str, chemin_sortie: str = None) -> str:
    """
    Prétraite une image pour améliorer la qualité OCR.

    Args:
        chemin_entree : chemin de l'image originale (.png, .jpg, .tiff, .bmp)
        chemin_sortie : chemin de sortie (optionnel, sinon génère automatiquement)

    Returns:
        chemin de l'image prétraitée
    """
    if not chemin_sortie:
        base, ext = os.path.splitext(chemin_entree)
        chemin_sortie = f"{base}_preprocessed.png"

    # Lecture de l'image
    img = cv2.imread(chemin_entree)
    if img is None:
        raise ValueError(f"Impossible de lire l'image : {chemin_entree}")

    # Pipeline de prétraitement
    img = _convertir_gris(img)
    img = _upscale_si_petit(img)
    img = _ameliorer_contraste(img)
    img = _debruiter(img)
    img = _redresser(img)
    img = _binariser(img)
    img = _supprimer_bordures(img)

    cv2.imwrite(chemin_sortie, img)
    return chemin_sortie


# ──────────────────────────────────────────────────────────────────────
# ÉTAPES DE PRÉTRAITEMENT
# ──────────────────────────────────────────────────────────────────────

def _convertir_gris(img: np.ndarray) -> np.ndarray:
    """Convertit en niveaux de gris si l'image est en couleur."""
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _upscale_si_petit(img: np.ndarray) -> np.ndarray:
    """
    Agrandit l'image si elle est trop petite pour un OCR fiable.
    Cible : largeur >= LARGEUR_MIN_PIXELS
    """
    h, w = img.shape[:2]
    if w < LARGEUR_MIN_PIXELS:
        facteur = LARGEUR_MIN_PIXELS / w
        nouvelle_w = int(w * facteur)
        nouvelle_h = int(h * facteur)
        img = cv2.resize(img, (nouvelle_w, nouvelle_h), interpolation=cv2.INTER_CUBIC)
    return img


def _ameliorer_contraste(img: np.ndarray) -> np.ndarray:
    """
    Améliore le contraste avec CLAHE (Contrast Limited Adaptive Histogram Equalization).
    Efficace sur les scans à faible contraste et les documents jaunis.
    """
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(img)


def _debruiter(img: np.ndarray) -> np.ndarray:
    """
    Supprime le bruit de l'image avec un filtre gaussien léger.
    Évite de trop flouter le texte.
    """
    return cv2.GaussianBlur(img, (3, 3), 0)


def _redresser(img: np.ndarray) -> np.ndarray:
    """
    Redresse automatiquement une image inclinée (deskew).
    Utilise la transformée de Hough pour détecter l'angle dominant.
    """
    try:
        # Détection des bords
        edges = cv2.Canny(img, 50, 150, apertureSize=3)

        # Transformée de Hough probabiliste
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180,
            threshold=100, minLineLength=100, maxLineGap=10
        )

        if lines is None or len(lines) == 0:
            return img

        # Calcul de l'angle médian
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 != x1:
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                # Filtrer les angles aberrants (garder seulement ±15°)
                if -15 < angle < 15:
                    angles.append(angle)

        if not angles:
            return img

        angle_median = np.median(angles)

        # Ne redresser que si l'inclinaison est significative (> 0.5°)
        if abs(angle_median) < 0.5:
            return img

        # Rotation de correction
        h, w = img.shape[:2]
        centre = (w // 2, h // 2)
        matrice = cv2.getRotationMatrix2D(centre, angle_median, 1.0)
        img_redressee = cv2.warpAffine(
            img, matrice, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE
        )
        return img_redressee

    except Exception:
        # En cas d'erreur, retourner l'image originale
        return img


def _binariser(img: np.ndarray) -> np.ndarray:
    """
    Binarise l'image en utilisant la meilleure méthode selon le contenu.

    - Otsu globale : pour les documents propres à contraste uniforme
    - Adaptative locale : pour les documents avec éclairage irrégulier (scans de mauvaise qualité)
    """
    # Essayer d'abord Otsu
    _, img_otsu = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Vérifier la qualité de la binarisation Otsu
    ratio_noir = np.sum(img_otsu == 0) / img_otsu.size
    if SCORE_BINARISATION <= ratio_noir <= 0.6:
        # Binarisation Otsu acceptable
        return img_otsu

    # Sinon utiliser la binarisation adaptative (meilleure pour scans dégradés)
    img_adapt = cv2.adaptiveThreshold(
        img, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=11,
        C=2
    )
    return img_adapt


def _supprimer_bordures(img: np.ndarray) -> np.ndarray:
    """
    Supprime les bordures noires parasites souvent présentes sur les scans.
    Détecte le contenu utile et recadre l'image.
    """
    try:
        # Inverser pour que le contenu soit blanc sur noir
        img_inv = cv2.bitwise_not(img)

        # Trouver les contours du contenu
        coords = cv2.findNonZero(img_inv)
        if coords is None:
            return img

        x, y, w, h = cv2.boundingRect(coords)

        # Ajouter une marge de 10 pixels
        marge  = 10
        x      = max(0, x - marge)
        y      = max(0, y - marge)
        x2     = min(img.shape[1], x + w + marge * 2)
        y2     = min(img.shape[0], y + h + marge * 2)

        return img[y:y2, x:x2]

    except Exception:
        return img


# ──────────────────────────────────────────────────────────────────────
# PRÉTRAITEMENT PDF → IMAGES
# ──────────────────────────────────────────────────────────────────────

def pretraiter_pdf_pages(chemin_pdf: str, dossier_sortie: str) -> list:
    """
    Convertit chaque page d'un PDF en image prétraitée.

    Nécessite : pdf2image (pip install pdf2image) + poppler

    Args:
        chemin_pdf    : chemin du fichier PDF
        dossier_sortie: dossier où sauvegarder les images

    Returns:
        liste des chemins des images prétraitées
    """
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise ImportError("pdf2image requis : pip install pdf2image")

    os.makedirs(dossier_sortie, exist_ok=True)

    # Conversion PDF → images à 300 DPI
    pages = convert_from_path(chemin_pdf, dpi=DPI_CIBLE)

    chemins_pretraites = []
    for i, page in enumerate(pages):
        # Sauvegarder la page comme PNG temporaire
        chemin_page = os.path.join(dossier_sortie, f"page_{i+1:03d}.png")
        page.save(chemin_page, "PNG")

        # Prétraiter la page
        chemin_pretraite = pretraiter_image(chemin_page)
        chemins_pretraites.append(chemin_pretraite)

        # Supprimer le temporaire
        if os.path.exists(chemin_page):
            os.remove(chemin_page)

    return chemins_pretraites


# ──────────────────────────────────────────────────────────────────────
# DIAGNOSTIC QUALITÉ
# ──────────────────────────────────────────────────────────────────────

def diagnostiquer_qualite(chemin_image: str) -> dict:
    """
    Analyse la qualité d'une image avant OCR et retourne un rapport.

    Returns:
        {
            "score":       0-100,
            "resolution":  "faible" | "correcte" | "bonne",
            "contraste":   "faible" | "correct" | "bon",
            "bruit":       "élevé" | "modéré" | "faible",
            "recommandation": "..."
        }
    """
    img = cv2.imread(chemin_image, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"score": 0, "erreur": "Image illisible"}

    h, w = img.shape
    score = 0
    rapport = {}

    # Résolution
    if w >= 2000:
        rapport["resolution"] = "bonne"
        score += 35
    elif w >= 1200:
        rapport["resolution"] = "correcte"
        score += 20
    else:
        rapport["resolution"] = "faible"
        score += 5

    # Contraste (écart-type des pixels)
    ecart_type = np.std(img)
    if ecart_type > 80:
        rapport["contraste"] = "bon"
        score += 35
    elif ecart_type > 40:
        rapport["contraste"] = "correct"
        score += 20
    else:
        rapport["contraste"] = "faible"
        score += 5

    # Bruit (Laplacien)
    laplacian_var = cv2.Laplacian(img, cv2.CV_64F).var()
    if laplacian_var > 500:
        rapport["bruit"] = "faible"
        score += 30
    elif laplacian_var > 100:
        rapport["bruit"] = "modéré"
        score += 15
    else:
        rapport["bruit"] = "élevé"
        score += 0

    rapport["score"] = score
    rapport["dimensions"] = f"{w}x{h} px"

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