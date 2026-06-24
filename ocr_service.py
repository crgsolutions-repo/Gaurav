import os
import re
import subprocess
import tempfile
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dateutil import parser as date_parser

from config import Config

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except ImportError:  # pragma: no cover - runtime fallback if Pillow is missing
    Image = ImageEnhance = ImageFilter = ImageOps = None


@dataclass
class OcrResult:
    text: str
    amount: float | None
    bill_date: date | None
    invoice_number: str | None
    vendor_name: str | None


class OcrServiceError(RuntimeError):
    pass


def resolved_tessdata_dir():
    configured = Path(Config.TESSDATA_PREFIX)
    if (configured / "eng.traineddata").exists():
        return configured

    nested_tessdata = configured / "tessdata"
    if (nested_tessdata / "eng.traineddata").exists():
        return nested_tessdata

    return configured


def ocr_diagnostics():
    executable = Path(Config.TESSERACT_EXE_PATH)
    tessdata_dir = resolved_tessdata_dir()
    eng_data = tessdata_dir / "eng.traineddata"
    return {
        "tesseract_executable": str(executable),
        "tessdata_prefix": str(tessdata_dir),
        "eng_traineddata_exists": eng_data.exists(),
    }


def log_ocr_diagnostics():
    diagnostics = ocr_diagnostics()
    logging.getLogger(__name__).info(
        "OCR configuration: tesseract_executable=%s; TESSDATA_PREFIX=%s; eng.traineddata exists=%s",
        diagnostics["tesseract_executable"],
        diagnostics["tessdata_prefix"],
        diagnostics["eng_traineddata_exists"],
    )


def _extract_amount_legacy(text):
    total_line_amounts = []
    all_amounts = []
    amount_pattern = re.compile(
        r"(?:rs\.?|inr|₹)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    )

    for line in text.splitlines():
        line_amounts = []
        for match in amount_pattern.finditer(line):
            value = float(match.group(1).replace(",", ""))
            if value > 0:
                line_amounts.append(value)
                all_amounts.append(value)

        lowered = line.lower()
        if line_amounts and any(token in lowered for token in ("grand total", "total", "amount due", "net amount")):
            total_line_amounts.extend(line_amounts)

    if total_line_amounts:
        return max(total_line_amounts)
    if all_amounts:
        return max(all_amounts)
    return None


def _extract_amount(text):
    grand_total_candidates = []
    payable_total_candidates = []
    fallback_total_candidates = []
    all_amounts = []
    amount_pattern = re.compile(
        r"(?:rs\.?|inr|₹)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    )

    for line_number, line in enumerate(text.splitlines()):
        line_amounts = []
        for match in amount_pattern.finditer(line):
            value = float(match.group(1).replace(",", ""))
            if value > 0:
                line_amounts.append(value)
                all_amounts.append(value)

        if not line_amounts:
            continue

        lowered = line.lower()
        is_tax_or_subtotal = any(
            token in lowered
            for token in ("sub-total", "subtotal", "sub total", "cgst", "sgst", "igst", "gst", "tax")
        )
        has_payable_label = any(
            token in lowered
            for token in (
                "grand total",
                "amount due",
                "amount payable",
                "net amount",
                "total payable",
                "balance due",
            )
        )
        has_total_label = re.search(r"\btotal\b", lowered) is not None

        if "grand total" in lowered and not is_tax_or_subtotal:
            grand_total_candidates.append((line_number, line_amounts[-1]))
        elif has_payable_label and not is_tax_or_subtotal:
            payable_total_candidates.append((line_number, line_amounts[-1]))
        elif has_total_label and not is_tax_or_subtotal:
            fallback_total_candidates.append((line_number, line_amounts[-1]))

    if grand_total_candidates:
        return grand_total_candidates[-1][1]
    if payable_total_candidates:
        return payable_total_candidates[-1][1]
    if fallback_total_candidates:
        return fallback_total_candidates[-1][1]
    if all_amounts:
        return all_amounts[-1]
    return None


def preprocess_receipt_image(image_path, output_path):
    if not Image:
        return Path(image_path)

    try:
        with Image.open(image_path) as image:
            prepared = ImageOps.grayscale(image)
            prepared = ImageEnhance.Contrast(prepared).enhance(2.0)
            prepared = prepared.filter(ImageFilter.SHARPEN)
            prepared = prepared.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=3))
            prepared = prepared.point(lambda pixel: 255 if pixel > 165 else 0, mode="1")
            prepared.save(output_path)
        return Path(output_path)
    except Exception:
        logging.getLogger(__name__).warning("Receipt preprocessing failed; using original image.", exc_info=True)
        return Path(image_path)


def _extract_date(text):
    patterns = [
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            try:
                return date_parser.parse(match.group(0), dayfirst=True, fuzzy=True).date()
            except (ValueError, TypeError, OverflowError):
                continue
    return None


def _extract_invoice_number(text):
    patterns = [
        r"\b(?:invoice|inv|bill|receipt)\s*(?:no|number|#|:)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})\b",
        r"\b(?:tax\s+invoice)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().upper()
    return None


def _extract_vendor_name(text):
    ignored = {"tax invoice", "invoice", "receipt", "bill", "cash memo"}
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" -:\t")
        if len(line) < 3:
            continue
        lowered = line.lower()
        if lowered in ignored:
            continue
        if re.search(r"\d{3,}", line):
            continue
        return line[:120]
    return None


def run_tesseract(image_path):
    executable = Path(Config.TESSERACT_EXE_PATH)
    if not executable.exists():
        raise OcrServiceError(f"Tesseract executable was not found at {executable}.")

    image = Path(image_path)
    if not image.exists():
        raise OcrServiceError("Receipt image was not found.")

    tessdata_dir = resolved_tessdata_dir()
    eng_data = tessdata_dir / "eng.traineddata"
    if not eng_data.exists():
        raise OcrServiceError(f"eng.traineddata was not found at {eng_data}.")

    env = os.environ.copy()
    env["TESSDATA_PREFIX"] = str(tessdata_dir)

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_base = str(Path(tmp_dir) / "receipt_ocr")
        processed_image = preprocess_receipt_image(image, Path(tmp_dir) / "receipt_preprocessed.png")
        command = [
            str(executable),
            str(processed_image),
            output_base,
            "-l",
            "eng",
            "--tessdata-dir",
            str(tessdata_dir),
            "--psm",
            "6",
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            env=env,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise OcrServiceError(completed.stderr.strip() or "Tesseract OCR failed.")

        text_path = Path(f"{output_base}.txt")
        text = text_path.read_text(encoding="utf-8", errors="ignore") if text_path.exists() else ""

    return OcrResult(
        text=text.strip(),
        amount=_extract_amount(text),
        bill_date=_extract_date(text),
        invoice_number=_extract_invoice_number(text),
        vendor_name=_extract_vendor_name(text),
    )
