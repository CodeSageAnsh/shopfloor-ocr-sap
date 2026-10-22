"""
test_carding.py — verify the carding server WITHOUT Telegram.

Runs the exact same two calls the bot makes (OCR -> preview) against a mill's
carding server, using a local image file. If this prints a review link that
opens in a browser, the bot's carding integration is proven good — any
remaining issue is just Telegram setup.

Usage (from this folder):
    python test_carding.py MILL_A  path\\to\\a\\carding_sheet.jpg
    python test_carding.py MILL_B  path\\to\\a\\carding_sheet.jpg
"""

import os
import sys

import config
import carding_client


def main():
    if len(sys.argv) != 3:
        print("Usage: python test_carding.py <MILL> <image_path>")
        print("  e.g.  python test_carding.py MILL_A sample.jpg")
        print("  MILL is one of:", ", ".join(config.MILLS))
        raise SystemExit(1)

    mill = sys.argv[1].upper()
    image_path = sys.argv[2]

    if mill not in config.MILLS:
        raise SystemExit(f"Unknown mill '{mill}'. Choose from: {', '.join(config.MILLS)}")
    if not os.path.isfile(image_path):
        raise SystemExit(f"Image not found: {image_path}")

    carding_base = config.MILLS[mill]["carding_base"]
    print(f"Mill:         {config.MILLS[mill]['label']}")
    print(f"Carding base: {carding_base}")
    print(f"Image:        {image_path}")
    print("-" * 60)

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_name = os.path.basename(image_path)

    print("[1/2] POST /api/ocr ...")
    try:
        ocr_data, api_used = carding_client.run_ocr(
            carding_base, image_bytes, image_name, config.REQUEST_TIMEOUT
        )
    except Exception as e:  # noqa: BLE001
        print("  FAILED:", e)
        print("  -> Is the carding server running and reachable at that URL?")
        raise SystemExit(1)
    print("  OK. api_used =", api_used)
    print("     date       =", ocr_data.get("date"))
    print("     shift      =", ocr_data.get("shift"))
    print("     supervisor =", ocr_data.get("supervisor"))

    print("[2/2] POST /api/carding_preview/create ...")
    try:
        session_id, link = carding_client.create_preview(
            carding_base, ocr_data, "telegram:TEST", config.REQUEST_TIMEOUT
        )
    except Exception as e:  # noqa: BLE001
        print("  FAILED:", e)
        raise SystemExit(1)
    print("  OK. session_id =", session_id)
    print()
    print("REVIEW LINK:")
    print("  " + link)
    print()
    print("Open that link in a browser. If it shows the carding preview page,")
    print("the bot's carding integration is fully working.")


if __name__ == "__main__":
    main()
