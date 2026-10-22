"""
image_prep.py  (v2) — geometric correction of carding sheet photos
==================================================================

WHY V2 EXISTS — THE ROW SHIFT WAS A CAMERA PROBLEM
--------------------------------------------------
Finisher Card values kept landing under the wrong material while Breaker and
Inter Card were correct. The cause was not the handwriting and not the prompt.

Measured on a real production sheet:

    sheet tilt in the photo                 -2.6 degrees
    distance from the material label to
      the Finisher columns                  676 px
    vertical drift caused by that tilt       31 px
    height of one material row               37 px

31 px of drift on a 37 px row means a number written in the Finisher column of
row 3 physically sits at the height of row 2 in the photograph. The model reads
the rows where they appear, so it assigns them one row too high - and the error
grows with distance from the label column, which is exactly why the leftmost
section was correct and the rightmost was not.

After the correction in this file the same sheet measures 6 px of drift on a
37 px row, which is safely inside the correct row.

The v1 bug this fixes: the old _skew_angle required ruled-line segments at
least 35% of the image width. On a tilted, WhatsApp-compressed photo the
longest detectable segment is about 24% of the width, so it found nothing and
silently applied no correction at all.

WHAT THIS FILE DOES, IN ORDER
-----------------------------
1. Honour the EXIF orientation flag.
2. Decide whether the sheet is genuinely rotated 90 degrees, from the direction
   the form's ruled lines run - not from the image aspect ratio, so a portrait
   photo of an upright sheet is left alone.
3. FLATTEN THE ROWS. Fit the angle of the near-horizontal ruled lines as a
   function of x, then apply the affine that makes them horizontal. This is the
   fix for the row shift.
4. Bring the image to a sensible pixel size.
5. Lift local contrast so faded pencil separates from the paper.

Dependencies: Pillow, opencv-python, numpy.  No Tesseract needed.

USAGE
-----
    from image_prep import prepare_b64
    image_b64, mime_type, report = prepare_b64(image_b64)
    print('[PREP]', report)

The report carries a 'before' and 'after' block, each with drift_px,
row_height_px, drift_rows and a plain-English verdict. If the 'after' verdict
says BAD, the photo is too crooked to trust and the sheet should be re-taken.
"""

import base64
import io
import math

import cv2
import numpy as np
from PIL import Image, ImageOps

# ── Tunables ─────────────────────────────────────────────────────────────────
MAX_DIM = 2400
MIN_DIM = 1500
JPEG_Q = 92

ROTATE_MARGIN = 1.25      # vertical rules must beat horizontal by this to turn
MIN_SEGMENTS = 8          # fewer ruled-line segments than this -> no correction
MAX_CORRECT_DEG = 15.0    # beyond this it is not a form photo, leave it alone

# The material label column sits at roughly 12% of the sheet width, the
# Finisher columns at roughly 75%. Drift is measured across that span.
LABEL_X_FRAC = 0.12
FINISHER_X_FRAC = 0.75


# ── Ruled-line helpers ───────────────────────────────────────────────────────
def _segments(gray, min_len_frac=0.10):
    """
    Near-horizontal ruled-line segments, as (mid_x, angle_degrees).
    min_len_frac is deliberately low: on a tilted photo a long ruled line is
    broken into short pieces by the Canny/Hough stage, so demanding a long
    segment finds nothing. This was the v1 bug.
    """
    h, w = gray.shape
    edges = cv2.Canny(gray, 50, 150)
    seg = cv2.HoughLinesP(edges, 1, math.pi / 360, threshold=60,
                          minLineLength=int(w * min_len_frac), maxLineGap=8)
    out = []
    if seg is None:
        return out
    for x1, y1, x2, y2 in seg[:, 0]:
        ang = math.degrees(math.atan2(int(y2) - int(y1), int(x2) - int(x1)))
        if abs(ang) <= MAX_CORRECT_DEG:
            out.append(((x1 + x2) / 2.0, ang))
    return out


def _row_height(gray):
    """Height of one material row, from the spacing of the long horizontal
    rules. Returns None when it cannot be determined."""
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 12)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(w * 0.10), 10), 1))
    profile = (cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel) > 0).sum(axis=1)
    strong = np.where(profile > w * 0.10)[0]
    if len(strong) < 3:
        return None
    bands = []
    for y in strong:
        if bands and y - bands[-1][-1] <= 6:
            bands[-1].append(y)
        else:
            bands.append([y])
    centres = [int(np.mean(b)) for b in bands]
    if len(centres) < 3:
        return None
    diffs = np.diff(centres)
    diffs = diffs[(diffs > 8) & (diffs < h * 0.3)]
    return float(np.median(diffs)) if len(diffs) else None


def _rule_line_lengths(gray):
    """Longest horizontal and vertical ruled-line run, on a downscaled copy."""
    h, w = gray.shape
    scale = 1000.0 / max(h, w)
    if scale < 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 25, 10)
    longest = {}
    for axis, shape in (("h", (40, 1)), ("v", (1, 40))):
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, shape)
        only = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(only, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        best = 0
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            best = max(best, cw if axis == "h" else ch)
        longest[axis] = best
    return longest["h"], longest["v"]


def _needs_quarter_turn(pil_img):
    """True when the ruled lines say the sheet is lying on its side. Uses the
    direction of the rules, never the image aspect ratio, so a portrait photo
    of an upright sheet is correctly left alone."""
    gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    h_len, v_len = _rule_line_lengths(gray)
    if max(h_len, v_len) < 80:
        return False, (h_len, v_len)
    if h_len == 0:
        return True, (h_len, v_len)
    return (v_len / max(h_len, 1)) > ROTATE_MARGIN, (h_len, v_len)


# ── The core correction ──────────────────────────────────────────────────────
def _measure_tilt(pil_img):
    """
    (angle_at_left_edge, angle_at_right_edge, n_segments) in degrees.
    A plain rotation gives two equal values. Photographing the sheet at an
    angle makes them differ, and that differential is what pushes the far
    columns into the wrong row.
    """
    gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    w = gray.shape[1]
    pts = _segments(gray)
    if len(pts) < MIN_SEGMENTS:
        return None, None, len(pts)
    xs = np.array([p[0] for p in pts], dtype=float)
    angles = np.array([p[1] for p in pts], dtype=float)
    if float(xs.max() - xs.min()) < w * 0.2:
        med = float(np.median(angles))
        return med, med, len(pts)
    slope, intercept = np.polyfit(xs, angles, 1)
    return float(intercept), float(slope * w + intercept), len(pts)


def _flatten_rows(pil_img):
    """
    Apply the affine that makes the form's ruled lines horizontal.
    Returns (image, info). On any failure the image comes back unchanged.
    """
    info = {"applied": False, "angle_left": None, "angle_right": None,
            "segments": 0, "correction_deg": 0.0}

    a_left, a_right, n = _measure_tilt(pil_img)
    info["segments"] = n
    if a_left is None:
        return pil_img, info

    info["angle_left"] = round(a_left, 2)
    info["angle_right"] = round(a_right, 2)
    mean_angle = (a_left + a_right) / 2.0

    if abs(mean_angle) < 0.3 or abs(mean_angle) > MAX_CORRECT_DEG:
        return pil_img, info

    src_bgr = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = src_bgr.shape[:2]

    # Shear vertically so a line at mean_angle becomes horizontal: the far edge
    # moves by tan(angle) * width while the near edge stays put. The pad keeps
    # every original pixel inside the output canvas.
    shift = math.tan(math.radians(mean_angle)) * w
    pad = abs(shift) if shift > 0 else 0.0
    src = np.float32([[0, 0], [w, 0], [0, h]])
    dst = np.float32([[0, pad], [w, pad - shift], [0, pad + h]])
    matrix = cv2.getAffineTransform(src, dst)
    out_h = int(h + abs(shift)) + 4
    warped = cv2.warpAffine(src_bgr, matrix, (w, out_h),
                            flags=cv2.INTER_CUBIC,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=(255, 255, 255))

    info["applied"] = True
    info["correction_deg"] = round(mean_angle, 2)
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)), info


def _drift_report(pil_img):
    """
    How far a Finisher-column value is displaced vertically relative to its own
    material label, in pixels and in row heights, with a plain verdict.
    """
    gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    w = gray.shape[1]
    a_left, a_right, n = _measure_tilt(pil_img)
    rh = _row_height(gray)
    if a_left is None or not rh:
        return {"drift_px": None, "row_height_px": rh, "drift_rows": None,
                "verdict": "could not measure"}
    mean_angle = (a_left + a_right) / 2.0
    span = w * (FINISHER_X_FRAC - LABEL_X_FRAC)
    drift = abs(math.tan(math.radians(mean_angle)) * span)
    ratio = drift / rh
    if ratio < 0.33:
        verdict = "OK - rows are safely aligned"
    elif ratio < 0.6:
        verdict = "MARGINAL - some values may land in the wrong row"
    else:
        verdict = "BAD - retake the photo squarely, rows will be misassigned"
    return {"drift_px": round(drift, 1), "row_height_px": round(rh, 1),
            "drift_rows": round(ratio, 2), "verdict": verdict}


# ── Contrast ─────────────────────────────────────────────────────────────────
def _boost_contrast(pil_img):
    """CLAHE on lightness only, so colour is preserved (blue pen versus grey
    pencil is a useful cue) while faint strokes gain separation."""
    lab = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return Image.fromarray(cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB))


# ── Entry point ──────────────────────────────────────────────────────────────
def prepare_for_ocr(raw, boost_contrast=True):
    """
    raw : image bytes, a base64 str, or a file path.
    Returns (jpeg_bytes, report).

    Never raises on a normal image problem. On internal failure it returns the
    original bytes with the reason in report['error'], so a preprocessing fault
    can never block an OCR run.
    """
    report = {"exif_applied": False, "rotated": 0, "flatten": None,
              "before": None, "after": None, "size_in": None, "size_out": None,
              "contrast_boosted": False, "error": None}
    original_bytes = None
    try:
        if isinstance(raw, str):
            if len(raw) > 400 and not raw.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".bmp")):
                raw = base64.b64decode(raw)
            else:
                with open(raw, "rb") as fh:
                    raw = fh.read()
        original_bytes = raw

        img = Image.open(io.BytesIO(raw))
        report["size_in"] = img.size

        # 1 ── EXIF orientation flag
        img = ImageOps.exif_transpose(img).convert("RGB")
        report["exif_applied"] = True

        # 2 ── genuine quarter turn, judged from the ruled lines
        turn, _ = _needs_quarter_turn(img)
        if turn:
            img = img.rotate(90, expand=True)
            report["rotated"] = 90
            still, _ = _needs_quarter_turn(img)
            if still:                      # made it worse, undo
                img = img.rotate(-90, expand=True)
                report["rotated"] = 0

        # 3 ── flatten the rows: THE fix for the Finisher row shift
        report["before"] = _drift_report(img)
        img, flat_info = _flatten_rows(img)
        report["flatten"] = flat_info
        report["after"] = _drift_report(img)

        # 4 ── sensible pixel size
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > MAX_DIM:
            f = MAX_DIM / long_edge
            img = img.resize((int(w * f), int(h * f)), Image.LANCZOS)
        elif long_edge < MIN_DIM:
            f = MIN_DIM / long_edge
            img = img.resize((int(w * f), int(h * f)), Image.LANCZOS)

        # 5 ── local contrast
        if boost_contrast:
            img = _boost_contrast(img)
            report["contrast_boosted"] = True

        report["size_out"] = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q, optimize=True)
        return buf.getvalue(), report

    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return (original_bytes if original_bytes is not None else raw), report


def prepare_b64(image_b64, boost_contrast=True):
    """Wrapper for app.py, which works in base64 throughout.
    Returns (new_b64, 'image/jpeg', report)."""
    clean, report = prepare_for_ocr(base64.b64decode(image_b64), boost_contrast)
    return base64.b64encode(clean).decode("ascii"), "image/jpeg", report