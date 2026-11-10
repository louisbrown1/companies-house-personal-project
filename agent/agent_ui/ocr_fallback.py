"""Bounded image-crop OCR agreement, without issuer-specific corrections."""

import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

MONEY = re.compile(r"([£$€])\s*([0-9OoIl][0-9OoIl,.]*)([mkb]?)(?!\w)")
CANDIDATE_MONEY = re.compile(r"([£$€E])\s*([0-9OoIl][0-9OoIl,.]*)([mkb]?)(?!\w)")
CONFUSABLE = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})


def uncertain(text, score=1):
    return score < 0.94 or any(re.search("[OoIl]", m[2]) for m in MONEY.finditer(text))


def canonical(text):
    text = CANDIDATE_MONEY.sub(lambda m: m[2].translate(CONFUSABLE) + m[3], text)
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def agreed_text(original, readings):
    """Require two valid image readings; preserve currency and all numeric values.

    Confusable mappings are used only to reject unrelated readings, never as the
    source of replacement digits. Each replacement must occur in both OCR outputs.
    """
    accepted = []
    original_money = list(MONEY.finditer(original))
    for text, score in readings:
        if score < 0.94 or canonical(text) != canonical(original):
            continue
        money = list(CANDIDATE_MONEY.finditer(text))
        if len(money) != len(original_money):
            continue
        if any(
            re.search("[OoIl]", b[2]) or a[2].translate(CONFUSABLE) != b[2] or a[3] != b[3]
            for a, b in zip(original_money, money)
        ):
            continue
        # Keep the source's currency sign, prose and geometry. Only uncertain
        # monetary digits corroborated by image readings are replaced.
        repaired = original
        for a, b in reversed(list(zip(original_money, money))):
            repaired = repaired[: a.start(2)] + b[2] + repaired[a.end(2) :]
        accepted.append(repaired)
    return next((text for text in accepted if accepted.count(text) >= 2), None)


def windows_read(images):
    if os.name != "nt":
        return []
    from PIL import ImageOps

    with tempfile.TemporaryDirectory(prefix="note-ocr-") as directory:
        root = Path(directory)
        paths = []
        for index, image in enumerate(images):
            for variant, rendered in enumerate(
                [image, ImageOps.autocontrast(image.convert("L")).convert("RGB")]
            ):
                # Windows OCR has a bounded image dimension.
                rendered.thumbnail((2500, 2500))
                path = root / f"{index}-{variant}.png"
                rendered.save(path)
                paths.append(str(path))
        inputs = root / "inputs.json"
        inputs.write_text(json.dumps(paths), encoding="utf-8")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                Path(__file__).with_name("windows_ocr.ps1").read_text(encoding="utf-8"),
            ],
            env={**os.environ, "OCR_INPUT_PATH": str(inputs)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
        texts = json.loads(result.stdout)
        if len(texts) != len(paths) or not all(isinstance(t, str) for t in texts):
            raise ValueError("Invalid OCR response")
        return [texts[i : i + 2] for i in range(0, len(texts), 2)]


def refine_crops(page, image, engine):
    """image is rendered at 4x; source boxes remain at the standard 2x scale."""
    from PIL import ImageFilter, ImageOps

    selected = [b for b in page["boxes"] if uncertain(b["text"], b.get("score", 1))][:40]
    crops = []
    for box in selected:
        xs = [p[0] * 2 for p in box["box"]]
        ys = [p[1] * 2 for p in box["box"]]
        crops.append(
            image.crop(
                (
                    max(0, min(xs) - 4),
                    max(0, min(ys) - 4),
                    min(image.width, max(xs) + 4),
                    min(image.height, max(ys) + 4),
                )
            )
        )
    if not crops:
        return page
    try:
        independent = windows_read(crops)
    except (OSError, ValueError, subprocess.SubprocessError):
        independent = []
    for index, (box, crop) in enumerate(zip(selected, crops)):
        readings = []
        method = "windows-image-variant-agreement"
        if independent:
            readings = [(text, 1) for text in independent[index]]
        repaired = agreed_text(box["text"], readings)
        if repaired is None:
            method = "rapidocr-image-variant-agreement"
            readings = []
            for variant in [
                crop,
                crop.resize((crop.width, int(crop.height * 1.25))),
                crop.resize((crop.width, int(crop.height * 1.5))),
                ImageOps.autocontrast(crop.convert("L"))
                .point(lambda p: 255 if p > 160 else 0)
                .convert("RGB"),
                crop.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)),
            ]:
                settings = {
                    key: getattr(engine, key, None) for key in ("use_det", "use_cls", "use_rec")
                }
                try:
                    result = engine(variant, use_det=False, use_cls=False)
                except Exception as exc:
                    # A failed fallback must not discard successful page OCR.
                    box.setdefault("fallback_errors", []).append(type(exc).__name__)
                    continue
                finally:
                    for key, value in settings.items():
                        if value is not None:
                            setattr(engine, key, value)
                if result.txts and len(result.txts) == 1:
                    readings.append((result.txts[0], float(result.scores[0])))
            repaired = agreed_text(box["text"], readings)
            if repaired is None and box.get("score", 1) < 0.94 and independent:
                # Severely garbled lines cannot be aligned to the first OCR.
                # Require agreement across two different recognizers instead.
                for text, score in readings:
                    if (
                        score >= 0.94
                        and not uncertain(text)
                        and len(text.split()) >= 5
                        and any(canonical(text) == canonical(other) for other in independent[index])
                    ):
                        repaired = text
                        method = "cross-engine-source-agreement"
                        readings = readings + [(t, 1) for t in independent[index]]
                        break
        if repaired is not None:
            box["fallback"] = {
                "method": method,
                "original_text": box["text"],
                "readings": [t for t, _ in readings],
                "verified": True,
            }
            box["text"] = repaired
    return page
