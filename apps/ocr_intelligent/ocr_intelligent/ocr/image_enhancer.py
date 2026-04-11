"""
ocr_intelligent/ocr/image_enhancer.py
═══════════════════════════════════════════════════════════════
Module d'amélioration d'images pour OCR intelligent (Frappe)
Formats : PDF · PNG · JPG · TIFF · BMP
═══════════════════════════════════════════════════════════════
"""

import os
import io
import frappe
from pathlib import Path


# ── Extensions supportées ────────────────────────────────────
EXTENSIONS_SUPPORTEES = {
    ".pdf":  "pdf",
    ".png":  "image",
    ".jpg":  "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".tif":  "image",
    ".bmp":  "image",
}


# ════════════════════════════════════════════════════════════
#  AMÉLIORATIONS D'IMAGE (OpenCV)
# ════════════════════════════════════════════════════════════

def defloutage(img_cv):
    """Défloutage par Unsharp Mask + renforcement Laplacien."""
    import cv2
    import numpy as np

    gaussian = cv2.GaussianBlur(img_cv, (9, 9), 10.0)
    img_sharp = cv2.addWeighted(img_cv, 1.8, gaussian, -0.8, 0)
    laplacian = cv2.Laplacian(img_sharp, cv2.CV_64F)
    laplacian = np.clip(laplacian, 0, 255).astype("uint8")
    return cv2.addWeighted(img_sharp, 1.0, laplacian, 0.3, 0)


def ameliorer_contraste(img_cv):
    """Amélioration contraste CLAHE adaptatif local."""
    import cv2

    if len(img_cv.shape) == 3:
        lab = cv2.cvtColor(img_cv, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    else:
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        return clahe.apply(img_cv)


def debruitage(img_cv):
    """Débruitage Non-Local Means (préserve les détails)."""
    import cv2

    if len(img_cv.shape) == 3:
        return cv2.fastNlMeansDenoisingColored(img_cv, None, 10, 10, 7, 21)
    return cv2.fastNlMeansDenoising(img_cv, None, 10, 7, 21)


def rotation_automatique(img_cv):
    """Détecte et corrige l'inclinaison du document (deskew)."""
    import cv2
    import numpy as np

    gris = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY) if len(img_cv.shape) == 3 else img_cv.copy()
    bords = cv2.Canny(gris, 50, 150, apertureSize=3)
    lignes = cv2.HoughLines(bords, 1, np.pi / 180, 200)

    if lignes is None:
        return img_cv

    angles = []
    for ligne in lignes[:20]:
        rho, theta = ligne[0]
        angle = np.degrees(theta) - 90
        if -45 < angle < 45:
            angles.append(angle)

    if not angles:
        return img_cv

    angle_median = float(np.median(angles))
    h, w = img_cv.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle_median, 1.0)
    return cv2.warpAffine(img_cv, M, (w, h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def binarisation(img_cv):
    """Binarisation adaptative Gaussienne (noir/blanc propre)."""
    import cv2

    gris = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY) if len(img_cv.shape) == 3 else img_cv
    binaire = cv2.adaptiveThreshold(
        gris, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=11, C=2
    )
    return cv2.cvtColor(binaire, cv2.COLOR_GRAY2BGR)


# ════════════════════════════════════════════════════════════
#  PIPELINE PRINCIPAL
# ════════════════════════════════════════════════════════════

def traiter_image_pil(img_pil, options: dict):
    """
    Applique le pipeline d'améliorations sur une image PIL.
    Retourne une image PIL améliorée.
    """
    import cv2
    import numpy as np
    from PIL import Image

    img_cv = cv2.cvtColor(np.array(img_pil.convert("RGB")), cv2.COLOR_RGB2BGR)

    if options.get("rotation",     True):  img_cv = rotation_automatique(img_cv)
    if options.get("debruitage",   True):  img_cv = debruitage(img_cv)
    if options.get("contraste",    True):  img_cv = ameliorer_contraste(img_cv)
    if options.get("defloutage",   True):  img_cv = defloutage(img_cv)
    if options.get("binarisation", False): img_cv = binarisation(img_cv)

    return Image.fromarray(cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB))


def ameliorer_fichier(
    chemin_entree: str,
    chemin_sortie: str = None,
    dpi: int = 300,
    defloutage_: bool = True,
    contraste: bool = True,
    debruitage_: bool = True,
    rotation: bool = True,
    binarisation_: bool = False,
) -> str:
    """
    Point d'entrée principal — améliore n'importe quel fichier supporté.

    Args:
        chemin_entree : Chemin absolu du fichier source
        chemin_sortie : Chemin de sortie (auto-généré si None)
        dpi           : Résolution cible en DPI (300 minimum)
        defloutage_   : Appliquer le défloutage
        contraste     : Améliorer le contraste (CLAHE)
        debruitage_   : Réduire le bruit (NLM)
        rotation      : Corriger l'inclinaison automatiquement
        binarisation_ : Convertir en noir/blanc strict

    Returns:
        str : Chemin du fichier amélioré
    """
    chemin_entree = Path(chemin_entree)
    ext = chemin_entree.suffix.lower()

    if ext not in EXTENSIONS_SUPPORTEES:
        frappe.throw(f"Extension non supportée : {ext}. "
                     f"Formats acceptés : {', '.join(EXTENSIONS_SUPPORTEES)}")

    if chemin_sortie is None:
        chemin_sortie = chemin_entree.parent / f"{chemin_entree.stem}_ameliore{ext}"
    chemin_sortie = Path(chemin_sortie)

    options = {
        "defloutage":  defloutage_,
        "contraste":   contraste,
        "debruitage":  debruitage_,
        "rotation":    rotation,
        "binarisation": binarisation_,
    }

    frappe.logger().info(f"[OCR] Amélioration : {chemin_entree.name} → {chemin_sortie.name}")

    if EXTENSIONS_SUPPORTEES[ext] == "pdf":
        _traiter_pdf(chemin_entree, chemin_sortie, options, dpi)
    else:
        _traiter_image(chemin_entree, chemin_sortie, options, dpi)

    frappe.logger().info(f"[OCR] ✅ Fichier amélioré : {chemin_sortie}")
    return str(chemin_sortie)


# ════════════════════════════════════════════════════════════
#  HANDLERS PAR TYPE
# ════════════════════════════════════════════════════════════

def _traiter_pdf(chemin_entree, chemin_sortie, options, dpi):
    """PDF → images haute résolution → améliorations → PDF avec OCR."""
    from pdf2image import convert_from_path
    from pypdf import PdfWriter, PdfReader
    import pytesseract

    pages = convert_from_path(str(chemin_entree), dpi=dpi, fmt="jpeg")
    frappe.logger().info(f"[OCR] {len(pages)} page(s) PDF converties à {dpi} DPI")

    writer = PdfWriter()
    for i, page_pil in enumerate(pages, 1):
        img_amelioree = traiter_image_pil(page_pil, options)
        pdf_data = pytesseract.image_to_pdf_or_hocr(
            img_amelioree, lang="fra+eng", extension="pdf",
            config=f"--dpi {dpi}"
        )
        reader = PdfReader(io.BytesIO(pdf_data))
        for p in reader.pages:
            writer.add_page(p)
        frappe.logger().info(f"[OCR]   Page {i}/{len(pages)} traitée")

    with open(chemin_sortie, "wb") as f:
        writer.write(f)


def _traiter_image(chemin_entree, chemin_sortie, options, dpi):
    """Image (PNG/JPG/TIFF/BMP) → améliorations → même format."""
    from PIL import Image

    img = Image.open(str(chemin_entree))
    img_amelioree = traiter_image_pil(img, options)

    ext = chemin_sortie.suffix.lower()
    params = {}
    if ext in (".jpg", ".jpeg"):
        params = {"quality": 95, "optimize": True}
    elif ext == ".png":
        params = {"optimize": True}
    elif ext in (".tiff", ".tif"):
        params = {"compression": "tiff_lzw", "dpi": (dpi, dpi)}

    img_amelioree.save(str(chemin_sortie), **params)


# ════════════════════════════════════════════════════════════
#  INTÉGRATION FRAPPE — API WHITELISTED
# ════════════════════════════════════════════════════════════

@frappe.whitelist()
def ameliorer_depuis_doctype(docname: str, dpi: int = 300, binarisation: bool = False):
    """
    API Frappe : améliore le fichier attaché à un OCR Document.

    Appel depuis JS :
        frappe.call({
            method: "ocr_intelligent.ocr.image_enhancer.ameliorer_depuis_doctype",
            args: { docname: frm.docname, dpi: 300 }
        });

    Args:
        docname     : Nom du document OCR Document
        dpi         : Résolution cible (défaut 300)
        binarisation: Activer la binarisation stricte

    Returns:
        dict : { "fichier_ameliore": chemin, "message": statut }
    """
    doc = frappe.get_doc("OCR Document", docname)

    if not doc.url_fichier:
        frappe.throw("Aucun fichier attaché à ce document.")

    # Résolution du chemin physique depuis l'URL Frappe
    chemin_relatif = doc.url_fichier.lstrip("/")
    chemin_absolu  = Path(frappe.get_site_path()) / chemin_relatif

    if not chemin_absolu.exists():
        frappe.throw(f"Fichier introuvable : {chemin_absolu}")

    # Lancement de l'amélioration
    chemin_ameliore = ameliorer_fichier(
        chemin_entree=str(chemin_absolu),
        dpi=int(dpi),
        binarisation_=bool(binarisation),
    )

    # Sauvegarde du chemin amélioré dans le document
    chemin_relatif_ameliore = "/" + str(Path(chemin_ameliore).relative_to(frappe.get_site_path()))
    doc.db_set("url_fichier", chemin_relatif_ameliore)
    doc.add_comment("Info", f"Fichier amélioré ({dpi} DPI) : {Path(chemin_ameliore).name}")

    return {
        "fichier_ameliore": chemin_relatif_ameliore,
        "message": f"✅ Fichier amélioré avec succès ({dpi} DPI)"
    }


@frappe.whitelist()
def ameliorer_lot(dpi: int = 300):
    """
    API Frappe : améliore tous les OCR Documents en statut 'En attente'.

    Appel depuis JS ou Scheduled Task :
        frappe.call({
            method: "ocr_intelligent.ocr.image_enhancer.ameliorer_lot"
        });
    """
    docs_en_attente = frappe.get_all(
        "OCR Document",
        filters={"statut": "En attente", "url_fichier": ["!=", ""]},
        pluck="name"
    )

    resultats = {"succes": [], "erreurs": []}

    for docname in docs_en_attente:
        try:
            res = ameliorer_depuis_doctype(docname, dpi=dpi)
            resultats["succes"].append(docname)
            frappe.logger().info(f"[OCR Lot] ✅ {docname}")
        except Exception as e:
            resultats["erreurs"].append({"doc": docname, "erreur": str(e)})
            frappe.logger().error(f"[OCR Lot] ❌ {docname} : {e}")

    frappe.logger().info(
        f"[OCR Lot] Terminé : {len(resultats['succes'])} succès, "
        f"{len(resultats['erreurs'])} erreurs"
    )
    return resultats