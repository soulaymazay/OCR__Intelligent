# -*- coding: utf-8 -*-
import cv2
import numpy as np
import os
import pytesseract

try:
    from paddleocr import PaddleOCR
    _PADDLE_AVAILABLE = True
except:
    _PADDLE_AVAILABLE = False

_paddle = None

def get_paddle():
    global _paddle
    if _paddle is not None:
        return _paddle

    if not _PADDLE_AVAILABLE:
        return None

    _paddle = PaddleOCR(
        use_angle_cls=False,   # ⚡ IMPORTANT
        lang="fr",
        show_log=False,
    )
    return _paddle


class OCREngine:

    def extraire_texte(self, path):

        if path.endswith(".pdf"):
            return self._pdf(path)

        return self._image(path)

    # ─────────────────────────────
    # IMAGE
    # ─────────────────────────────

    def _image(self, path):
        img = cv2.imread(path)
        if img is None:
            return self._vide()

        img = self._resize(img)

        # ⚡ PADDLE FIRST
        paddle = get_paddle()
        if paddle:
            res = paddle.ocr(img)
            texte = self._extract_paddle(res)

            if len(texte.split()) > 5:
                return {
                    "texte": texte,
                    "score_confiance": 85,
                    "moteur": "paddle"
                }

        # 🔥 FALLBACK TESSERACT LIGHT
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3,3), 0)

        texte = pytesseract.image_to_string(gray, lang="fra+eng")

        return {
            "texte": texte,
            "score_confiance": 60,
            "moteur": "tesseract"
        }

    # ─────────────────────────────
    # PDF
    # ─────────────────────────────

    def _pdf(self, path):
        from pdf2image import convert_from_path

        pages = convert_from_path(path, first_page=1, last_page=2)

        textes = []
        for p in pages:
            img = cv2.cvtColor(np.array(p), cv2.COLOR_RGB2BGR)
            res = self._image_array(img)
            textes.append(res["texte"])

        return {
            "texte": "\n".join(textes),
            "score_confiance": 80,
            "moteur": "pdf"
        }

    def _image_array(self, img):
        img = self._resize(img)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        texte = pytesseract.image_to_string(gray)
        return {"texte": texte}

    # ─────────────────────────────
    # HELPERS
    # ─────────────────────────────

    def _resize(self, img):
        h, w = img.shape[:2]

        if w < 1000:
            scale = 1400 / w
            return cv2.resize(img, None, fx=scale, fy=scale)

        if w > 2000:
            scale = 1800 / w
            return cv2.resize(img, None, fx=scale, fy=scale)

        return img

    def _extract_paddle(self, result):
        textes = []
        for page in result:
            for line in page:
                textes.append(line[1][0])
        return "\n".join(textes)

    def _vide(self):
        return {"texte": "", "score_confiance": 0}
    # ─────────────────────────────
# SINGLETON ENGINE (FIX ERREUR)
# ─────────────────────────────

_ENGINE = None

def get_engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = OCREngine()
    return _ENGINE