"""
material_rows.py — label-driven material row mapping
====================================================

THE PROBLEM THIS SOLVES
-----------------------
The carding form does not always carry the same material rows. Sale Yarn is
often absent, the order varies, and only the qualities that actually ran get
written. The old prompt handed Claude six fixed JSON keys and asked it to work
out which key each row belonged to. When it guessed wrong, an entire row of
machine numbers landed under the wrong material - silently, with valid SAP
codes, and nothing downstream could detect it.

THE FIX
-------
Claude no longer sees the six keys at all. It returns a LIST of rows, each
carrying the material label exactly as handwritten. Python then maps each label
to one of the six keys. A label Python cannot recognise produces a loud warning
instead of misplaced production data.

Row shift stops being a thing that can happen.
"""

import re

# ── The six canonical keys your whole codebase already uses ──────────────────
MATERIALS = ['hessian', 'sacking_warp', 'sacking_weft',
             'sale_yarn', '12lb_warp', '15lb_weft']

SEC_SIZES = {'BC': 8, 'IC': 5, 'FC': 10, 'TC': 2, 'HW': 1}


def _empty_materials():
    """A full six-material structure with every slot blank."""
    return {
        mk: {sec: {f'M{i}': '' for i in range(1, cnt + 1)}
             for sec, cnt in SEC_SIZES.items()}
        for mk in MATERIALS
    }


# ── Label matching ───────────────────────────────────────────────────────────
def _clean(label):
    """Lowercase, strip punctuation, collapse whitespace."""
    s = str(label or '').lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def match_material(label):
    """
    Map a handwritten material label to one of the six keys.
    Returns None if it cannot be matched confidently.

    Rules are checked in priority order. 'hessian warp' must resolve to
    hessian, not sacking_warp, so hessian is tested first. The weight-based
    rows are tested before the sacking rows because '12 lbs warp' contains
    'warp' but is not a sacking row.
    """
    s = _clean(label)
    if not s:
        return None

    if 'hessian' in s or 'hessain' in s or 'hesian' in s:
        return 'hessian'

    if 'sale' in s and ('yarn' in s or 'yam' in s):
        return 'sale_yarn'

    # Weight-prefixed rows. Accept "12 lbs warp", "12lb warp", "12 LBS WARP".
    # The lookarounds mean the digits must not be part of a longer number, so
    # "125" and "112" are correctly rejected, but "12lb" still matches.
    if re.search(r'(?<!\d)12(?!\d)', s):
        return '12lb_warp'
    if re.search(r'(?<!\d)15(?!\d)', s):
        return '15lb_weft'
    # The SAP master calls the 15 lb row "13 Lb Heavy Warp & Weft" - accept it
    # in case a supervisor copies the SAP wording onto the paper.
    if re.search(r'(?<!\d)13(?!\d)', s) and 'heavy' in s:
        return '15lb_weft'

    if 'sack' in s:
        if 'weft' in s or 'wept' in s or 'weptt' in s:
            return 'sacking_weft'
        if 'warp' in s or 'wrap' in s:
            return 'sacking_warp'

    return None


# ── The normaliser ───────────────────────────────────────────────────────────
def normalize_materials(parsed):
    """
    Convert parsed['rows'] (list, label-carrying) into parsed['materials']
    (the six-key dict the rest of the codebase expects).

    Returns (parsed, warnings) where warnings is a list of human-readable
    strings. An empty list means every row on the sheet was placed cleanly.

    Backwards compatible: if the model returns the old 'materials' dict and no
    'rows' list, the data is passed through untouched.
    """
    warnings = []

    rows = parsed.get('rows')
    if not isinstance(rows, list):
        # Old-style response, or a model that ignored the instruction.
        if 'materials' not in parsed:
            parsed['materials'] = _empty_materials()
            warnings.append(
                'OCR returned neither "rows" nor "materials" - no material data extracted.'
            )
        return parsed, warnings

    materials = _empty_materials()
    placed = {}          # canonical key -> the label that claimed it

    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            warnings.append(f'Row {idx}: not an object, skipped.')
            continue

        label = row.get('label_as_written', '')
        key = match_material(label)

        if key is None:
            warnings.append(
                f'Row {idx}: material label "{label}" not recognised - '
                f'this row was NOT saved. Check the form and correct it.'
            )
            continue

        if key in placed:
            warnings.append(
                f'Row {idx}: "{label}" maps to {key}, but row "{placed[key]}" '
                f'already claimed it. Second row NOT saved - two rows cannot be '
                f'the same material.'
            )
            continue

        placed[key] = label

        # Copy the section slots across, ignoring anything unexpected.
        for sec, cnt in SEC_SIZES.items():
            src = row.get(sec) or {}
            if not isinstance(src, dict):
                continue
            for i in range(1, cnt + 1):
                slot = f'M{i}'
                val = src.get(slot, '')
                materials[key][sec][slot] = '' if val is None else str(val).strip()

    parsed['materials'] = materials

    # Record which materials were actually present, so the preview page can say
    # "Sale Yarn not on this sheet" rather than showing a mysterious blank row.
    parsed['materials_present'] = sorted(placed.keys())
    parsed['materials_absent'] = [m for m in MATERIALS if m not in placed]

    if not placed:
        warnings.append('No material rows could be mapped - nothing was extracted.')

    return parsed, warnings


# ── The prompt ───────────────────────────────────────────────────────────────
OCR_PROMPT = """You are OCR for a textile mill carding department daily routing form.

HEADER FIELDS:
- Date: written DD/MM/YY. 02/08/26 = 2nd August 2026. Output as DD-MM-YYYY.
- Shift: a single letter A, B or C. Often handwritten loose near the top centre
  of the form rather than inside a labelled box. Look for it there.
- Supervisor: the name written next to "Supervisor:". Copy the name exactly as written.

COLUMN SECTIONS (always in this order, left to right):
- BREAKER CARD  "BC": M1 to M8
- INTER CARD    "IC": M1 to M5
- FINISHER CARD "FC": M1 to M10
- TEASER CARD   "TC": M1, M2
- HW TEASER     "HW": M1

MATERIAL ROWS - THIS IS THE MOST IMPORTANT INSTRUCTION:
The number of material rows CHANGES from sheet to sheet. Only the qualities
that actually ran that shift are written. Rows appear in different orders, some
materials are missing entirely, and there is a numbered "Sl." column you must
ignore.

Therefore: do NOT try to fit rows into a fixed list of materials. Instead,
return one object per row that has ANY handwriting in it, in the order the rows
appear on the paper, top to bottom. For each row, copy the material name from
the "Basic Material" column EXACTLY as it is handwritten, into
"label_as_written". Do not correct spelling, do not expand abbreviations, do
not translate it into a name you think it should be. If the row reads
"12 lbs warp", write "12 lbs warp".

Skip rows that are completely blank. Do not invent a row for a material that is
not written on the sheet.

HOW THE CELLS WORK:
The slot name (M1, M2, M3...) is only the printed column position. The value
handwritten in that slot is the actual MACHINE NUMBER that ran that material.
Slot M3 containing "15" means machine 15, not machine 3.

DURATION NOTATION - a bracketed fraction means a part shift:
- 2        machine 2 ran the full shift        -> "2"
- 2(1/2)   machine 2 ran half a shift          -> "2(1/2)"
- 15(1/4)  machine 15 ran a quarter shift      -> "15(1/4)"
- 15(3/4)  machine 15 ran three quarters       -> "15(3/4)"
- blank cell                                   -> ""
NEVER split a machine number from its fraction. "2(1/2)" is ONE value. The
fraction is often written directly BELOW the number instead of beside it - that
is still one value, not two.

SHED PREFIX - a leading letter marks which shed a machine belongs to:
Some numbers start with a letter - "o" or "O" for the OLD shed, "n" or "N" for
the NEW shed - for example "o2", "N15", "o2(1/2)", "n28(3/4)". Keep the letter
EXACTLY as written and attached to its number: "o2" stays "o2", "n15(1/4)" stays
"n15(1/4)". Do NOT drop the letter, and do NOT turn it into a digit (an "o" is
the letter o, not a zero). If a number has no leading letter, output just the
number as normal.

MULTIPLE MACHINES IN ONE CELL:
A slot often holds two or more machine numbers stacked above one another. Join
them with ", " and keep each exactly as written:
- "15" above "28(1/2)"    -> "15, 28(1/2)"
- "16", "20", "23"        -> "16, 20, 23"
- "22" above "23"         -> "22, 23"

CROWDED HANDWRITING - READ THIS CAREFULLY:
On busy rows the numbers are cramped and it is easy to attach a value to the
wrong column, or to attach a fraction to the wrong number. Work column by
column using the PRINTED VERTICAL RULED LINES as your boundary. For each slot,
ask: which printed column does this mark actually sit inside? Do not let a
value drift into a neighbouring slot.

If you cannot tell which of two adjacent slots a number belongs to, put it in
the slot it overlaps most and reduce the "machines" confidence score. Never
invent a number to fill a slot.

ROW ALIGNMENT - THE MOST COMMON FAILURE ON THIS FORM:
The supervisor's handwriting often drifts UPWARD in the Finisher Card section,
so a row's numbers sit high inside their cell, close to the ruled line above.
Do not follow the apparent height of the ink.

For every row, work like this: start at the handwritten material name in the
"Basic Material" column, identify the two horizontal ruled lines that box that
name in, then trace those same two lines all the way across the form to the
right edge. Every value you assign to that row must sit between those two
lines. The material name is your anchor - not the vertical position of the
numbers.

Before you finish, sanity-check each row. If one row has far more Finisher
values than the row above it, you have probably pulled a row's worth of numbers
up by one. Re-read that section against the ruled lines.

A machine can only run one shift's worth of hours in one shift. If you find
yourself writing the same machine number at full shift under two different
materials, you have almost certainly assigned one of them to the wrong row.
Go back and check it against the ruled lines.

ILLEGIBLE CELLS:
Output "?" rather than guessing. A "?" takes the operator two seconds to fix.
A confident wrong number goes into SAP unnoticed.

NON-NUMERIC CELLS:
If a cell holds a short word or code rather than a number, store it exactly as
written and lower the "materials" confidence.

STOPPAGE SECTION (bottom of the form):
Headed "Reason if any tagged machine was down (write only if stoppage is 30 min
or more)". Lines read roughly:

    B/Card NO.15    Idle,    9.40 AM - 10.25 AM    Leather Roll change

For each line return:
- "machine"    : as written, keeping the section prefix and the "NO." number,
                 e.g. "B/Card NO.15".
- "start_time" : normalise to "H:MM AM" or "H:MM PM" using a COLON. The form
                 writes times with a dot ("9.40 AM"); convert to "9:40 AM".
                 A dot will not parse downstream.
- "end_time"   : same colon format. "2.00 pm" -> "2:00 PM".
- "reason"     : the actual reason, e.g. "Leather Roll change". Words like
                 "Idle" describe the state, not the reason.
Return an empty list if there are no stoppage lines.

CONFIDENCE:
Be honest. Score low where handwriting overlaps or is faint. A low score sends
the sheet to a human for checking, which is the correct outcome. Do not inflate
the score.

Return ONLY valid JSON. No markdown fences, no commentary.

{
  "date": "DD-MM-YYYY",
  "shift": "A",
  "supervisor": "name",
  "rows": [
    {
      "label_as_written": "exact handwritten material name",
      "BC": {"M1":"","M2":"","M3":"","M4":"","M5":"","M6":"","M7":"","M8":""},
      "IC": {"M1":"","M2":"","M3":"","M4":"","M5":""},
      "FC": {"M1":"","M2":"","M3":"","M4":"","M5":"","M6":"","M7":"","M8":"","M9":"","M10":""},
      "TC": {"M1":"","M2":""},
      "HW": {"M1":""}
    }
  ],
  "stoppages": [{"machine":"","start_time":"","end_time":"","reason":""}],
  "confidence": {"overall":85,"date":90,"shift":95,"supervisor":80,"machines":85,"materials":80,"stoppages":75}
}

Include one object in "rows" for every non-blank material row on the sheet, in
top-to-bottom order. If the sheet has three material rows, return three
objects. If it has five, return five."""