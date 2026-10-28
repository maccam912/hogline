"""Bounded local OCR; no external models, credentials, or inference services."""

import csv
import io
import shutil
import subprocess
import time

import cv2
import numpy as np
from PIL import Image


class TesseractOCR:
    def __init__(self, deadline=None):
        self.deadline = deadline

    @property
    def available(self):
        return shutil.which("tesseract") is not None

    def read(self, image: Image.Image, *, whitelist="0123456789", psm=7, preprocessing="otsu"):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError("OCR worker time budget reached; retained evidence can be analyzed later")
        rgb = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        if preprocessing == "otsu":
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        elif preprocessing == "gray":
            binary = gray
        else:
            raise ValueError("Unknown OCR preprocessing mode")
        if np.mean(binary) < 127:
            binary = 255 - binary
        binary = cv2.copyMakeBorder(binary, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        buffer = io.BytesIO()
        Image.fromarray(binary).save(buffer, format="PNG")
        try:
            result = subprocess.run(
                [
                    "tesseract",
                    "stdin",
                    "stdout",
                    "--psm",
                    str(psm),
                    "-c",
                    f"tessedit_char_whitelist={whitelist}",
                    "tsv",
                ],
                input=buffer.getvalue(),
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Local OCR failed or timed out; no text was inferred") from exc
        words = [
            r
            for r in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter="\t")
            if r.get("text", "").strip() and float(r["conf"]) >= 0
        ]
        return {
            "preprocessing": preprocessing,
            "psm": psm,
            "text": " ".join(r["text"] for r in words),
            "engine_confidence": min((float(r["conf"]) for r in words), default=None),
            "confidence_kind": "tesseract_engine_score_not_calibrated_probability",
            "tokens": [{"text": r["text"], "confidence": float(r["conf"])} for r in words],
        }


def normalized_crop(image, bounds):
    left, top, right, bottom = bounds
    return image.crop(
        (
            round(left * image.width),
            round(top * image.height),
            round(right * image.width),
            round(bottom * image.height),
        )
    )
