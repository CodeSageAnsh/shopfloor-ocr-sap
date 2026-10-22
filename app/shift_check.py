"""
shift_check.py — arithmetic validation of a carding sheet
=========================================================

WHY THIS EXISTS
---------------
MCH_MASTER tells you whether a machine EXISTS and is SWITCHED ON. It cannot tell you
whether the machine was written on that row of the paper. When OCR shifts a row
or misreads a digit into another valid machine number, every code still comes
back green and the bad data flows into SAP unnoticed.

This module catches that class of error using nothing but arithmetic on the
data itself.

THE RULE
--------
A machine can only run for one shift's worth of hours in one shift. The form
encodes part-shifts as fractions, and they are designed to add up:

    030BC002:  2(1/2)  + 2(1/2)   = 8 hrs   legitimate split
    030BC015:  15(1/4) + 15(3/4)  = 8 hrs   legitimate split

So any machine whose hours across the whole sheet exceed 8 is an OCR error,
guaranteed. On a real shifted sheet this fires 10-15 times at once, which makes
the failure impossible to miss.

It also flags the same machine appearing twice inside one material + section,
which cannot happen on a correct sheet regardless of hours.
"""

import re
from collections import defaultdict

SHIFT_LENGTH_HRS = 8

# Fraction written on the form -> hours actually run
FRACTION_HRS = {'1': 8, '3/4': 6, '1/2': 4, '1/4': 2}

SAP_PREFIX = {'BC': '030BC', 'IC': '030IC', 'FC': '030FF',
              'TC': '030TC', 'HW': '030HC'}

SEC_SIZES = {'BC': 8, 'IC': 5, 'FC': 10, 'TC': 2, 'HW': 1}

SECTION_NAME = {'BC': 'Breaker', 'IC': 'Inter', 'FC': 'Finisher',
                'TC': 'Teaser', 'HW': 'HW'}

MATERIAL_NAME = {
    'hessian': 'Hessian', 'sacking_warp': 'Sacking Warp',
    'sacking_weft': 'Sacking Weft', 'sale_yarn': 'Sale Yarn',
    '12lb_warp': '12 Lb Warp', '15lb_weft': '15 Lb Weft',
}


def parse_cell(raw, section):
    """
    Turn one cell's written value into a list of (sap_code, hours).
    Handles "15", "15(1/2)", "15, 28(1/2)", and shed-prefixed "o15" / "n15".
    A leading o/O -> old shed (031), n/N -> new shed (032), none -> 030, so
    o2 and n2 are kept as DIFFERENT machines. Anything else is ignored here and
    reported separately by find_unparseable_cells().
    """
    out = []
    base = SAP_PREFIX.get(section)
    if not base:
        return out
    suf = base[3:]
    for part in str(raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^([onON]?)(\d+)(?:\((\d+/\d+)\))?$', part)
        if not m:
            continue
        letter = m.group(1).lower()
        num = int(m.group(2))
        frac = m.group(3)
        d = '1' if letter == 'o' else '2' if letter == 'n' else '0'
        code = f"03{d}{suf}{num:03d}"
        hrs = FRACTION_HRS.get(frac, SHIFT_LENGTH_HRS) if frac else SHIFT_LENGTH_HRS
        out.append((code, hrs))
    return out


def validate_shift_hours(parsed):
    """
    Check a parsed OCR result for arithmetic impossibilities.
    Returns a list of human-readable warning strings. Empty list = clean.
    Never raises.
    """
    warnings = []
    try:
        materials = parsed.get('materials') or {}
        if not isinstance(materials, dict):
            return warnings

        hours = defaultdict(float)
        places = defaultdict(list)

        for mk, secs in materials.items():
            if not isinstance(secs, dict):
                continue
            for sec, slots in secs.items():
                if sec not in SAP_PREFIX or not isinstance(slots, dict):
                    continue

                seen_here = {}
                for i in range(1, SEC_SIZES.get(sec, 0) + 1):
                    slot = f'M{i}'
                    raw = slots.get(slot, '')
                    for code, hrs in parse_cell(raw, sec):
                        hours[code] += hrs
                        places[code].append(
                            f"{MATERIAL_NAME.get(mk, mk)} {SECTION_NAME.get(sec, sec)} {slot}"
                        )
                        if code in seen_here:
                            warnings.append(
                                f"{code} appears twice in {MATERIAL_NAME.get(mk, mk)} "
                                f"{SECTION_NAME.get(sec, sec)} ({seen_here[code]} and {slot}) "
                                f"- one machine cannot occupy two slots in the same section."
                            )
                        seen_here[code] = slot

        over = sorted((c, h) for c, h in hours.items() if h > SHIFT_LENGTH_HRS)
        for code, h in over:
            warnings.append(
                f"{code} totals {h:g} hrs in an {SHIFT_LENGTH_HRS}-hour shift "
                f"({' + '.join(places[code])}) - impossible, OCR has duplicated it."
            )

        # A large cluster of over-booked machines in one section is the
        # signature of a whole row being shifted, so say so plainly.
        if len(over) >= 4:
            by_section = defaultdict(int)
            for code, _ in over:
                for sec, pfx in SAP_PREFIX.items():
                    if code[3:].startswith(pfx[3:]):
                        by_section[sec] += 1
            worst = max(by_section, key=by_section.get)
            warnings.insert(
                0,
                f"LIKELY ROW SHIFT: {len(over)} machines are over-booked, "
                f"{by_section[worst]} of them in the {SECTION_NAME.get(worst, worst)} "
                f"section. Check whether an entire {SECTION_NAME.get(worst, worst)} "
                f"row has been read against the wrong material. DO NOT push to SAP "
                f"until this is resolved."
            )

    except Exception as exc:
        warnings.append(f"hour check failed to run: {type(exc).__name__}: {exc}")

    return warnings


def find_unparseable_cells(parsed):
    """
    Report cells that hold something other than a machine number, so they are
    not silently dropped on the way to SAP. Returns a list of strings.
    """
    out = []
    try:
        materials = parsed.get('materials') or {}
        for mk, secs in materials.items():
            if not isinstance(secs, dict):
                continue
            for sec, slots in secs.items():
                if sec not in SAP_PREFIX or not isinstance(slots, dict):
                    continue
                for i in range(1, SEC_SIZES.get(sec, 0) + 1):
                    slot = f'M{i}'
                    raw = str(slots.get(slot, '') or '').strip()
                    if not raw:
                        continue
                    if not parse_cell(raw, sec):
                        out.append(
                            f"{MATERIAL_NAME.get(mk, mk)} {SECTION_NAME.get(sec, sec)} "
                            f"{slot} contains {raw!r} - no SAP code can be built from "
                            f"this, it will be dropped."
                        )
    except Exception as exc:
        out.append(f"unparseable-cell check failed: {exc}")
    return out