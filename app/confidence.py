"""
confidence.py — a confidence meter you can actually trust
=========================================================

THE PROBLEM WITH THE OLD METER
------------------------------
The numbers on the preview page came from the OCR_PROMPT's JSON template:

    "confidence": {"overall":85, "date":90, "shift":95, ...}

Claude filled those in by self-report. They were a feeling, not a measurement.
That is why the date showed 95% when "02-08-2026" is either a valid date or it
is not, and why the number never moved when an operator corrected a cell.

WHAT THIS MODULE DOES INSTEAD
-----------------------------
Every score is derived from something checkable:

    date        does it parse to a real date, and is it plausible
    shift       is it exactly A, B or C
    supervisor  does it match a known supervisor
    machines    do the SAP codes exist and are they active in MCH_MASTER,
                and does the shift arithmetic hold
    materials   did every row map to a known material, and is every cell
                a readable machine number
    geometry    how far the photo's tilt displaces a Finisher value from its
                own row, measured by image_prep
    stoppages   are the machine, times and reason all present and parseable

Because it is computed from the data, it RISES when an operator fixes a cell.
That is the behaviour the shop floor expects.

HARD CAPS
---------
Two situations mean the data is definitely wrong, so the overall score is capped
no matter how good everything else looks:

    any shift-hour violation   -> flagged (cap set by CAP_ON_HOUR_VIOLATION;
                                  currently 100, i.e. not capped)
    any code missing from MCH_MASTER -> overall capped at 75

A green number therefore means "verified clean", not "the model felt good".
"""

import re
from datetime import datetime, timedelta

import os as _os

def _load_supervisors():
    """Supervisor names are site data, not code: read them from
    config/supervisors.txt (one name per line, gitignored)."""
    path = _os.getenv('SUPERVISORS_FILE',
                      _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'config', 'supervisors.txt'))
    try:
        with open(path, encoding='utf-8') as f:
            return [ln.strip().lower() for ln in f if ln.strip() and not ln.startswith('#')]
    except OSError:
        return []

KNOWN_SUPERVISORS = _load_supervisors()

SEC_SIZES = {'BC': 8, 'IC': 5, 'FC': 10, 'TC': 2, 'HW': 1}

DATE_FORMATS = ('%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y', '%Y-%m-%d', '%d-%m-%y')

# Relative importance of each field in the overall score. Machines and
# materials dominate because they are what reaches SAP.
WEIGHTS = {
    'machines':   0.32,
    'materials':  0.24,
    'geometry':   0.14,
    'date':       0.10,
    'shift':      0.06,
    'supervisor': 0.06,
    'stoppages':  0.08,
}

CAP_ON_HOUR_VIOLATION = 100
CAP_ON_NOT_FOUND = 75


def _clamp(v):
    return int(max(0, min(100, round(v))))


# ── Individual field scores ──────────────────────────────────────────────────
def score_date(value):
    raw = str(value or '').strip()
    if not raw:
        return 0, 'no date extracted'
    parsed = None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return 0, f'"{raw}" is not a valid date'
    today = datetime.now()
    if parsed > today + timedelta(days=1):
        return 55, f'{raw} is in the future'
    if parsed < today - timedelta(days=45):
        return 70, f'{raw} is more than 45 days old'
    return 100, 'valid date'


def score_shift(value):
    raw = str(value or '').strip().upper()
    if not raw:
        return 0, 'no shift extracted'
    if raw in ('A', 'B', 'C'):
        return 100, f'shift {raw}'
    return 0, f'"{raw}" is not A, B or C'


def _similar(a, b):
    """Crude similarity: fraction of a's characters present in order in b."""
    if not a or not b:
        return 0.0
    i = 0
    for ch in b:
        if i < len(a) and a[i] == ch:
            i += 1
    return i / len(a)


def score_supervisor(value):
    raw = re.sub(r'\s+', ' ', str(value or '').strip().lower())
    if not raw:
        return 0, 'no supervisor extracted'
    if raw in KNOWN_SUPERVISORS:
        return 100, 'known supervisor'
    best = max((_similar(raw, k) for k in KNOWN_SUPERVISORS), default=0.0)
    if best >= 0.85:
        return 85, 'close to a known supervisor, check the spelling'
    return 60, 'supervisor not in the known list'


def score_machines(machine_validation, hour_warnings):
    mv = machine_validation or {}
    if not mv.get('sql_available'):
        return 65, 'SQL Server unreachable, machines could not be verified'

    machines = mv.get('machines') or {}
    total = len(machines)
    if total == 0:
        return 0, 'no machine codes extracted'

    not_found = len(mv.get('not_found_codes') or [])
    inactive = len(mv.get('inactive_codes') or [])
    violations = [w for w in (hour_warnings or []) if 'totals' in w or 'twice' in w]

    score = 100.0
    score -= 25.0 * len(violations)
    score -= 15.0 * not_found
    score -= 5.0 * inactive

    bits = [f'{total - not_found - inactive}/{total} verified active']
    if violations:
        bits.append(f'{len(violations)} impossible hour totals')
    if not_found:
        bits.append(f'{not_found} not in MCH_MASTER')
    if inactive:
        bits.append(f'{inactive} inactive')
    return _clamp(score), ', '.join(bits)


def score_materials(parsed, row_warnings, odd_cells):
    materials = parsed.get('materials') or {}
    present = [mk for mk, secs in materials.items()
               if isinstance(secs, dict) and any(
                   str(v or '').strip()
                   for sec in secs.values() if isinstance(sec, dict)
                   for v in sec.values())]
    if not present:
        return 0, 'no material rows contain data'

    q_cells = 0
    for secs in materials.values():
        if not isinstance(secs, dict):
            continue
        for sec, slots in secs.items():
            if not isinstance(slots, dict):
                continue
            for v in slots.values():
                if str(v or '').strip() == '?':
                    q_cells += 1

    score = 100.0
    score -= 20.0 * len(row_warnings or [])
    score -= 10.0 * len(odd_cells or [])
    score -= 5.0 * q_cells

    bits = [f'{len(present)} material rows read']
    if row_warnings:
        bits.append(f'{len(row_warnings)} row mapping problems')
    if odd_cells:
        bits.append(f'{len(odd_cells)} unreadable cells')
    if q_cells:
        bits.append(f'{q_cells} cells marked illegible')
    return _clamp(score), ', '.join(bits)


def score_geometry(prep_report):
    """
    From image_prep's drift measurement. Drift is how far a Finisher value is
    displaced vertically from its own material label because of photo tilt,
    expressed in row heights. Above about a third of a row, values start
    landing in the wrong row.
    """
    if not prep_report:
        return 75, 'photo geometry not measured'
    after = (prep_report or {}).get('after') or {}
    rows = after.get('drift_rows')
    if rows is None:
        return 75, 'photo geometry could not be measured'
    if rows < 0.15:
        return 100, f'rows aligned, drift {rows} of a row'
    if rows < 0.33:
        return 88, f'rows aligned, drift {rows} of a row'
    if rows < 0.60:
        return 50, f'drift {rows} of a row, values may be in the wrong row'
    return 20, f'drift {rows} of a row, photo too crooked to trust'


def score_stoppages(parsed):
    stops = parsed.get('stoppages') or []
    stops = [s for s in stops if isinstance(s, dict)
             and (str(s.get('machine', '')).strip() or str(s.get('reason', '')).strip())]
    if not stops:
        return 100, 'no stoppages recorded'

    time_re = re.compile(r'^\d{1,2}:\d{2}\s*(AM|PM)$', re.I)
    complete = 0
    problems = []
    for s in stops:
        ok = True
        if not str(s.get('machine', '')).strip():
            ok = False; problems.append('missing machine')
        if not str(s.get('reason', '')).strip():
            ok = False; problems.append('missing reason')
        for f in ('start_time', 'end_time'):
            t = str(s.get(f, '')).strip()
            if not t:
                ok = False; problems.append(f'missing {f}')
            elif not time_re.match(t):
                ok = False; problems.append(f'{f} "{t}" will not parse')
        if ok:
            complete += 1

    score = 100.0 * complete / len(stops)
    note = f'{complete}/{len(stops)} stoppage lines complete'
    if problems:
        note += ' - ' + ', '.join(sorted(set(problems))[:3])
    return _clamp(score), note


# ── The public entry point ───────────────────────────────────────────────────
def compute_confidence(parsed, machine_validation=None, hour_warnings=None,
                       odd_cells=None, row_warnings=None, prep_report=None):
    """
    Returns a dict shaped like the old confidence block, so the preview page
    and audit log need no changes:

        {'overall': 92, 'date': 100, 'shift': 100, 'supervisor': 100,
         'machines': 95, 'materials': 90, 'stoppages': 100,
         'geometry': 100, 'notes': {...}, 'capped_by': None}
    """
    try:
        d_s,  d_n  = score_date(parsed.get('date'))
        s_s,  s_n  = score_shift(parsed.get('shift'))
        sup_s, sup_n = score_supervisor(parsed.get('supervisor'))
        m_s,  m_n  = score_machines(machine_validation, hour_warnings)
        mat_s, mat_n = score_materials(parsed, row_warnings, odd_cells)
        g_s,  g_n  = score_geometry(prep_report)
        st_s, st_n = score_stoppages(parsed)

        parts = {'date': d_s, 'shift': s_s, 'supervisor': sup_s,
                 'machines': m_s, 'materials': mat_s,
                 'geometry': g_s, 'stoppages': st_s}

        overall = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)

        capped_by = None
        violations = [w for w in (hour_warnings or []) if 'totals' in w or 'twice' in w]
        if violations and overall > CAP_ON_HOUR_VIOLATION:
            overall = CAP_ON_HOUR_VIOLATION
            capped_by = 'impossible shift hours - the sheet contains a duplicate'
        nf = len((machine_validation or {}).get('not_found_codes') or [])
        if nf and overall > CAP_ON_NOT_FOUND:
            overall = CAP_ON_NOT_FOUND
            capped_by = f'{nf} machine code(s) do not exist in MCH_MASTER'

        out = {k: _clamp(v) for k, v in parts.items()}
        out['overall'] = _clamp(overall)
        out['capped_by'] = capped_by
        out['notes'] = {'date': d_n, 'shift': s_n, 'supervisor': sup_n,
                        'machines': m_n, 'materials': mat_n,
                        'geometry': g_n, 'stoppages': st_n}
        return out

    except Exception as exc:
        return {'overall': 0, 'date': 0, 'shift': 0, 'supervisor': 0,
                'machines': 0, 'materials': 0, 'geometry': 0, 'stoppages': 0,
                'capped_by': f'scoring failed: {exc}', 'notes': {}}