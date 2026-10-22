"""
carding_client.py — thin HTTP client for the carding server's API.

This replicates EXACTLY what the WhatsApp bot does for carding, so this
Telegram bot plugs into the same, UNCHANGED carding server (app.py at
CARDING_BASE:5001):

  1. POST {carding_base}/api/ocr
        JSON: { "image": <base64>, "mimeType": "image/jpeg"|"image/png",
                "imageName": <name> }
        -> { "success": true, "data": { "date", "shift", "supervisor", ... },
             "apiUsed": ... }

  2. POST {carding_base}/api/carding_preview/create
        JSON: { "ocr_data": <the `data` object from step 1>, "jid": <id> }
        -> { "session_id": <id> }

  Preview link: {carding_base}/carding_preview?s=<session_id>

The operator opens that link to review, machine-master-validate, and push to SAP —
exactly the same web page the WhatsApp flow used.
"""

import base64
import requests


class CardingError(Exception):
    """Raised when the carding server reports a failure (e.g. OCR failed)."""


def _mime_for(filename: str) -> str:
    return "image/png" if filename.lower().endswith(".png") else "image/jpeg"


def run_ocr(carding_base, image_bytes, image_name, timeout):
    """Step 1 — send the image, get the OCR data object back."""
    payload = {
        "image": base64.b64encode(image_bytes).decode("ascii"),
        "mimeType": _mime_for(image_name),
        "imageName": image_name,
    }
    resp = requests.post(f"{carding_base}/api/ocr", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise CardingError(data.get("error") or "Carding OCR failed")
    return data["data"], data.get("apiUsed")


def create_preview(carding_base, ocr_data, jid, timeout):
    """Step 2 — turn OCR data into a review session, return (id, link)."""
    payload = {"ocr_data": ocr_data, "jid": jid}
    resp = requests.post(
        f"{carding_base}/api/carding_preview/create", json=payload, timeout=timeout
    )
    resp.raise_for_status()
    data = resp.json()
    session_id = data.get("session_id")
    if not session_id:
        raise CardingError("No session_id from carding server: " + str(data))
    return session_id, f"{carding_base}/carding_preview?s={session_id}"


def process_photo(carding_base, image_bytes, image_name, jid, timeout, plant=None):
    """Full flow: OCR -> preview session. Returns (summary_dict, review_link).

    `plant` is the mill picked in Telegram ('MILL_A' / 'MILL_B'). We stamp it onto
    the OCR data so the carding server opens the preview on the right mill and
    applies the right rules (for MILL_B it flags any number missing its o/n prefix).
    """
    ocr_data, api_used = run_ocr(carding_base, image_bytes, image_name, timeout)
    if plant and isinstance(ocr_data, dict):
        ocr_data["plant"] = plant
    session_id, link = create_preview(carding_base, ocr_data, jid, timeout)
    summary = {
        "date": ocr_data.get("date"),
        "shift": ocr_data.get("shift"),
        "supervisor": ocr_data.get("supervisor"),
        "api_used": api_used,
    }
    return summary, link
