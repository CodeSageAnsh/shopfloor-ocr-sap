"""
app.py — Carding OCR System v3
================================
Tech Stack:
  - Frontend  : HTML + Vanilla JS (dark theme)
  - Backend   : Python + Flask
  - Database  : MongoDB (local) — auto-falls back to SQLite if MongoDB not running
  - OCR       : Claude Sonnet (primary) -> Gemini Flash (fallback)
  - Export    : openpyxl — one sheet, 6 rows per record (one per material)
  - SQL Server: MCH_MASTER machine active/inactive validation
"""

import os, json, io, time, traceback, subprocess, sys, threading, re as _re
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from flask import Flask, request, jsonify, render_template, send_file, session
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
import requests as http_req
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import sqlite3

import audit_log
from audit_log import Action, Status

from material_rows import OCR_PROMPT, normalize_materials
from shift_check   import validate_shift_hours, find_unparseable_cells
from confidence import compute_confidence

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']
CORS(app)

CLAUDE_API_KEY = os.getenv('CLAUDE_API_KEY', 'YOUR_CLAUDE_KEY_HERE')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', 'YOUR_GEMINI_KEY_HERE')
AUDIT_PASSWORD = os.environ['AUDIT_PASSWORD']
AUDIT_HASH     = generate_password_hash(AUDIT_PASSWORD)
MONGO_URI      = os.getenv('MONGODB_URI', 'mongodb://localhost:27017')
MONGO_DB       = os.getenv('MONGODB_DB',  'carding_ocr')

# ══════════════════════════════════════════════════════════════════════════════
# SQL SERVER — MCH_MASTER machine validator
# ══════════════════════════════════════════════════════════════════════════════
SQL_SERVER   = os.getenv('SQL_SERVER', '')
SQL_USERNAME = os.getenv('SQL_USERNAME', '')
SQL_PASSWORD = os.getenv('SQL_PASSWORD', '')
SQL_DATABASE = os.getenv('SQL_DATABASE', '')

_mch_cache       = {}          # CODE(upper) -> {active, mch_type, des, mtype}
_mch_cache_lock  = threading.Lock()
_mch_loaded      = False


def _sql_db_for_codes(sap_codes):
    """Pick the machine-master database from the machine-code prefix.
    030xxx -> MILL_A;  031xxx / 032xxx -> MILL_B;
    035xxx -> MILL_C.
    Same server & credentials for all — only the database name differs."""
    for c in sap_codes:
        cc = str(c).strip().upper()
        if cc.startswith('031') or cc.startswith('032'):
            return os.getenv('SQL_DATABASE_MILL_B', SQL_DATABASE)
        if cc.startswith('035'):
            return os.getenv('SQL_DATABASE_MILL_C', SQL_DATABASE)
    return SQL_DATABASE   # MILL_A / default (from env SQL_DATABASE)


def _sql_connect(database=None):
    import pyodbc
    available = pyodbc.drivers()
    preferred = ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server',
                 'ODBC Driver 13 for SQL Server', 'SQL Server Native Client 11.0', 'SQL Server']
    driver = next((d for d in preferred if d in available), available[0] if available else 'ODBC Driver 17 for SQL Server')
    db = database or SQL_DATABASE
    conn_str = (f"DRIVER={{{driver}}};SERVER={SQL_SERVER};DATABASE={db};"
                f"UID={SQL_USERNAME};PWD={SQL_PASSWORD};TrustServerCertificate=yes;Encrypt=no;Connect Timeout=10;")
    return pyodbc.connect(conn_str)


def _mch_load(force=False):
    """Load all DEPT=03 machines from MCH_MASTER into memory cache."""
    global _mch_cache, _mch_loaded
    with _mch_cache_lock:
        if _mch_loaded and not force:
            return True
        try:
            conn   = _sql_connect()
            cursor = conn.cursor()
            cursor.execute("SELECT MchType, CODE, DES, Mtype, Active FROM MCH_MASTER")
            rows   = cursor.fetchall()
            conn.close()
            cache  = {}
            for row in rows:
                code = str(row[1] or '').strip().upper()
                if code:
                    cache[code] = {
                        'active':   str(row[4] or '').strip().upper() == 'Y',
                        'mch_type': str(row[0] or '').strip(),
                        'des':      str(row[2] or '').strip(),
                        'mtype':    str(row[3] or '').strip(),
                    }
            _mch_cache  = cache
            _mch_loaded = True
            print(f'[SQL] ✅ MCH_MASTER loaded: {len(cache)} machines')
            return True
        except Exception as e:
            print(f'[SQL] ❌ MCH_MASTER load failed: {e}')
            _mch_loaded = False
            return False


def _mch_check(sap_codes: list):
    """Live SQL check — no cache."""
    result = {'sql_available': False, 'machines': {}, 'inactive_codes': [], 'not_found_codes': []}
    if not sap_codes:
        result['sql_available'] = True
        return result

    codes_upper = [c.strip().upper() for c in sap_codes if c.strip()]
    try:
        conn   = _sql_connect(_sql_db_for_codes(codes_upper))
        cursor = conn.cursor()
        placeholders = ','.join('?' for _ in codes_upper)
        cursor.execute(
            f"SELECT MchType, CODE, DES, Mtype, Active FROM MCH_MASTER "
            f"WHERE UPPER(LTRIM(RTRIM(CODE))) IN ({placeholders})",
            codes_upper
        )
        found = {}
        for row in cursor.fetchall():
            code = str(row[1] or '').strip().upper()
            found[code] = {
                'active':   str(row[4] or '').strip().upper() == 'Y',
                'mch_type': str(row[0] or '').strip(),
                'des':      str(row[2] or '').strip(),
                'mtype':    str(row[3] or '').strip(),
            }
        conn.close()
        result['sql_available'] = True
        for code in codes_upper:
            info = found.get(code)
            result['machines'][code] = info
            if info is None:
                result['not_found_codes'].append(code)
            elif not info['active']:
                result['inactive_codes'].append(code)
    except Exception as e:
        print(f'[SQL] Live check error: {e}')
    return result


def _sap_prefix_map(plant=None, shed=None):
    """Shed/mill-aware SAP machine-code prefixes.
    MILL_A (and any non-MILL_B plant) -> 030.
    MILL_B old shed -> 031, new shed -> 032.  MILL_C -> 035."""
    plant = str(plant or 'MILL_A').upper()
    if plant == 'MILL_B':
        d = '2' if str(shed or '').lower() in ('new', '2', '032') else '1'
    elif plant == 'MILL_C':
        d = '5'
    else:
        d = '0'
    return {'BC': f'03{d}BC', 'IC': f'03{d}IC', 'FC': f'03{d}FF',
            'TC': f'03{d}TC', 'HW': f'03{d}HC'}


def _code_from_token(part, section, plant=None):
    """Parse one written machine token into a SAP work-center code.
    MILL_B only: leading letter o/O = old shed (031), n/N = new shed (032).
    Letter on any other mill -> None (blocked). MILL_C -> 035.
    No letter: MILL_A / any other plant -> 030; MILL_B -> None (shed not
    specified, so the token is skipped and the operator must fix it)."""
    suffix = {'BC': 'BC', 'IC': 'IC', 'FC': 'FF', 'TC': 'TC', 'HW': 'HC'}.get(section)
    if not suffix:
        return None
    m = _re.match(r'^\s*([onON]?)\s*(\d+)', str(part).strip())
    if not m:
        return None
    letter = m.group(1).lower()
    num = int(m.group(2))
    plant_u = str(plant or 'MILL_A').upper()
    # o/n shed letters are valid ONLY for MILL_B. On any other mill (MILL_A, MILL_C)
    # a letter is a writing error: skip it so the preview blocks confirm.
    if letter and plant_u != 'MILL_B':
        return None
    if plant_u == 'MILL_C':
        return f"035{suffix}{num:03d}"   # MILL_C single shed -> always 035
    if letter == 'o':
        d = '1'
    elif letter == 'n':
        d = '2'
    elif str(plant or '').upper() == 'MILL_B':
        return None   # MILL_B number with no o/n prefix -> operator must specify shed
    else:
        d = '0'
    return f"03{d}{suffix}{num:03d}"


def _extract_sap_codes_from_ocr(ocr_data: dict) -> list:
    """Extract all SAP codes from a full OCR result dict."""
    plant = ocr_data.get('plant')
    SEC_SIZES  = {'BC': 8, 'IC': 5, 'FC': 10, 'TC': 2, 'HW': 1}
    codes = set()
    mat_data = ocr_data.get('materials', {})
    for mk, ms in mat_data.items():
        if not isinstance(ms, dict):
            continue
        for sec, slots in ms.items():
            if sec not in SEC_SIZES or not isinstance(slots, dict):
                continue
            for i in range(1, SEC_SIZES.get(sec, 0) + 1):
                raw = slots.get(f'M{i}', '')
                if not raw:
                    continue
                for part in str(raw).split(','):
                    code = _code_from_token(part, sec, plant)
                    if code:
                        codes.add(code)
    return list(codes)


# Pre-load in background so first request is instant
threading.Thread(target=_mch_load, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════════
# MongoDB / SQLite
# ══════════════════════════════════════════════════════════════════════════════
_mongo_client = None
_sqlite_mode  = False

def get_mongo():
    global _mongo_client, _sqlite_mode
    if _sqlite_mode:
        return None
    try:
        if _mongo_client is None:
            from pymongo import MongoClient
            _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
            _mongo_client.server_info()
            print('[DB] MongoDB connected:', MONGO_URI)
        return _mongo_client[MONGO_DB]
    except Exception as e:
        print(f'[DB] MongoDB unavailable ({e}) — using SQLite fallback')
        _sqlite_mode = True
        return None

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'carding_data.db')

def _sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_sqlite():
    conn = _sqlite()
    conn.execute('''CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, shift TEXT, supervisor TEXT,
        breaker_card TEXT, inter_card TEXT, finisher_card TEXT,
        teaser_card TEXT, hw TEXT, materials TEXT, stoppages TEXT,
        saved_at TEXT DEFAULT (datetime("now","localtime")),
        image_file TEXT
    )''')
    conn.commit(); conn.close()

db = get_mongo()
if db is None:
    _init_sqlite()
audit_log.init_audit_table(get_mongo())

MATERIALS = ['hessian','sacking_warp','sacking_weft','sale_yarn','12lb_warp','15lb_weft']
MATERIAL_LABELS = {
    'hessian':'Hessian','sacking_warp':'Sacking Warp','sacking_weft':'Sacking Weft',
    'sale_yarn':'Sale Yarn','12lb_warp':'12 Lb Warp','15lb_weft':'15 Lb Weft',
}


def _strip_fences(text):
    clean = text.strip()
    if '```' in clean:
        for p in clean.split('```'):
            p = p.strip().lstrip('json').strip()
            if p.startswith('{'):
                return p
    return clean

def call_claude(image_b64, mime_type):
    if not CLAUDE_API_KEY or CLAUDE_API_KEY == 'YOUR_CLAUDE_KEY_HERE':
        raise RuntimeError('Claude API key not set in .env')
    t0 = time.time()
    try:
        resp = http_req.post('https://api.anthropic.com/v1/messages',
            headers={'Content-Type':'application/json','x-api-key':CLAUDE_API_KEY,'anthropic-version':'2023-06-01'},
            json={'model':'claude-sonnet-4-6','max_tokens':2500,'messages':[{'role':'user','content':[
                {'type':'image','source':{'type':'base64','media_type':mime_type,'data':image_b64}},
                {'type':'text','text':OCR_PROMPT}
            ]}]}, timeout=60)
    except http_req.exceptions.Timeout: raise RuntimeError('Claude timed out')
    except http_req.exceptions.ConnectionError as e: raise RuntimeError(f'Claude network: {e}')
    ms = int((time.time()-t0)*1000)
    if not resp.content: raise RuntimeError(f'Claude empty (HTTP {resp.status_code})')
    if resp.status_code != 200:
        try: err = resp.json().get('error',{}).get('message',resp.text[:200])
        except: err = resp.text[:200]
        raise RuntimeError(f'Claude HTTP {resp.status_code}: {err}')
    try: data = resp.json()
    except: raise RuntimeError(f'Claude non-JSON: {resp.text[:200]}')
    if 'error' in data: raise RuntimeError(f"Claude: {data['error'].get('message','?')}")
    text = ''.join(b.get('text','') for b in data.get('content',[]))
    try: parsed = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e: raise RuntimeError(f'Claude JSON: {e} raw:{text[:200]}')
    tok = data.get('usage',{})
    cost = round((tok.get('input_tokens',1000)*3 + tok.get('output_tokens',500)*15)/1_000_000*83, 4)
    return parsed, cost, ms

def call_gemini(image_b64, mime_type):
    if not GEMINI_API_KEY or GEMINI_API_KEY == 'YOUR_GEMINI_KEY_HERE':
        raise RuntimeError('Gemini API key not set in .env')
    t0 = time.time()
    try:
        resp = http_req.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}',
            json={'contents':[{'parts':[{'inline_data':{'mime_type':mime_type,'data':image_b64}},{'text':OCR_PROMPT}]}],
                  'generationConfig':{'maxOutputTokens':8192,'temperature':0.1,
                                      'thinkingConfig':{'thinkingBudget':0}}}, timeout=60)
    except http_req.exceptions.Timeout: raise RuntimeError('Gemini timed out')
    except http_req.exceptions.ConnectionError as e: raise RuntimeError(f'Gemini network: {e}')
    ms = int((time.time()-t0)*1000)
    if not resp.content: raise RuntimeError(f'Gemini empty (HTTP {resp.status_code})')
    if resp.status_code != 200:
        try: err = resp.json().get('error',{}).get('message',resp.text[:200])
        except: err = resp.text[:200]
        raise RuntimeError(f'Gemini HTTP {resp.status_code}: {err}')
    try: data = resp.json()
    except: raise RuntimeError(f'Gemini non-JSON: {resp.text[:200]}')
    if 'error' in data: raise RuntimeError(f"Gemini: {data['error'].get('message','?')}")
    try: text = data['candidates'][0]['content']['parts'][0]['text']
    except: raise RuntimeError(f'Gemini structure: {str(data)[:200]}')
    try: parsed = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e: raise RuntimeError(f'Gemini JSON: {e} raw:{text[:200]}')
    return parsed, 0.0, ms

def db_insert(data):
    mdb = get_mongo()
    if mdb is not None:
        data['saved_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        r = mdb.records.insert_one(data)
        return str(r.inserted_id)
    conn = _sqlite()
    conn.execute('''INSERT INTO records
        (date,shift,supervisor,breaker_card,inter_card,finisher_card,teaser_card,hw,materials,stoppages,image_file)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
        data.get('date',''), data.get('shift',''), data.get('supervisor',''),
        json.dumps(data.get('breaker_card',{})), json.dumps(data.get('inter_card',{})),
        json.dumps(data.get('finisher_card',{})), json.dumps(data.get('teaser_card',{})),
        json.dumps(data.get('hw',{})), json.dumps(data.get('materials',{})),
        json.dumps(data.get('stoppages',[])), data.get('imageFile','')
    ))
    conn.commit()
    rid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close(); return str(rid)

def db_get_all():
    mdb = get_mongo()
    if mdb is not None:
        raw = list(mdb.records.find({}).sort('saved_at',-1))
        for r in raw:
            r['id'] = str(r.pop('_id'))
        return raw
    conn = _sqlite()
    rows = conn.execute('SELECT * FROM records ORDER BY saved_at DESC').fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        for f in ('breaker_card','inter_card','finisher_card','teaser_card','hw','stoppages'):
            if isinstance(d.get(f), str):
                try: d[f] = json.loads(d[f])
                except: pass
        if isinstance(d.get('materials'), str):
            try: d['materials'] = json.loads(d['materials'])
            except: pass
        if isinstance(d.get('materials'), dict) and 'written' in d['materials']:
            d['sap_data'] = d['materials'].get('sap', {})
            d['materials'] = d['materials'].get('written', {})
        result.append(d)
    return result

def db_delete(rid):
    mdb = get_mongo()
    if mdb is not None:
        try:
            from bson import ObjectId
            mdb.records.delete_one({'_id': ObjectId(rid)})
        except: mdb.records.delete_one({'_id': rid})
        return
    conn = _sqlite()
    conn.execute('DELETE FROM records WHERE id=?', (rid,))
    conn.commit(); conn.close()

@app.route('/')
def index(): return render_template('index.html')



@app.route('/api/ocr', methods=['POST'])
def ocr_extract():
    try:
        body      = request.get_json(force=True)
        image_b64 = body.get('image', '')
        mime_type = body.get('mimeType', 'image/jpeg')
        shift_ov  = body.get('shiftOverride', '')
        date_ov   = body.get('dateOverride', '')
        img_name  = body.get('imageName', '')

        if not image_b64:
            return jsonify({'success': False, 'error': 'No image data'}), 400

        # ── IMAGE PREP ── straighten / upscale / boost contrast before OCR ────
        from image_prep import prepare_b64
        image_b64, mime_type, prep_report = prepare_b64(image_b64)
        print('[PREP]', prep_report)

        audit_log.log_event(Action.OCR_STARTED, details={'image': img_name}, request=request)

        parsed = None; api_used = None; fallback = False
        cost = 0; ms = 0; errs = []

        # ── Claude first ──────────────────────────────────────────────────────
        if CLAUDE_API_KEY and CLAUDE_API_KEY != 'YOUR_CLAUDE_KEY_HERE':
            try:
                parsed, cost, ms = call_claude(image_b64, mime_type)
                api_used = 'claude'
            except Exception as e:
                errs.append(f'Claude: {e}')
        else:
            errs.append('Claude: key not set')

        # ── Gemini fallback ───────────────────────────────────────────────────
        if parsed is None:
            if GEMINI_API_KEY and GEMINI_API_KEY != 'YOUR_GEMINI_KEY_HERE':
                try:
                    parsed, cost, ms = call_gemini(image_b64, mime_type)
                    api_used = 'gemini'; fallback = True
                except Exception as e:
                    errs.append(f'Gemini: {e}')
            else:
                errs.append('Gemini: key not set')

        if parsed is None:
            audit_log.log_event(Action.OCR_FAILED, Status.FAILED,
                                details={'errors': errs}, request=request)
            return jsonify({'success': False, 'error': ' | '.join(errs),
                            'tip': 'Open /api/diagnose'}), 500

        # ── Manual overrides from the UI win over whatever OCR read ───────────
        if shift_ov:
            parsed['shift'] = shift_ov
        if date_ov:
            parsed['date'] = date_ov

        # ── Map the label-carrying rows onto the six material keys ────────────
        # This is what stops a row of machine numbers landing under the wrong
        # material when the sheet's row order changes.
        parsed, row_warnings = normalize_materials(parsed)
        if row_warnings:
            print('[ROWS]', *row_warnings, sep='\n  ')

        # ── Arithmetic validation — catches row shifts MCH_MASTER cannot see ────────
        hour_warnings = validate_shift_hours(parsed)
        if hour_warnings:
            print('[HOURS]', *hour_warnings, sep='\n  ')

        # ── Cells that hold something other than a machine number ─────────────
        odd_cells = find_unparseable_cells(parsed)
        if odd_cells:
            print('[CELLS]', *odd_cells, sep='\n  ')

        # ── Machine validation against MCH_MASTER (exists / active) ─────────────────
        machine_validation = {'sql_available': False, 'machines': {},
                              'inactive_codes': [], 'not_found_codes': []}
        try:
            codes = _extract_sap_codes_from_ocr(parsed)
            if codes:
                machine_validation = _mch_check(codes)
        except Exception as ve:
            print(f'[SQL] Validation error: {ve}')

        parsed['confidence'] = compute_confidence(
            parsed,
            machine_validation=machine_validation,
            hour_warnings=hour_warnings,
            odd_cells=odd_cells,
            row_warnings=row_warnings,
            prep_report=prep_report,
        )
        print('[SCORE]', parsed['confidence']['overall'], '%',
              '| held by:', parsed['confidence']['capped_by'])

        # ── Audit trail ───────────────────────────────────────────────────────
        audit_log.log_event(
            Action.OCR_FALLBACK if fallback else Action.OCR_SUCCESS, Status.SUCCESS,
            details={'api': api_used, 'errors': errs,
                     'row_warnings': row_warnings,
                     'hour_warnings': hour_warnings},
            request=request,
            prod_date=parsed.get('date'), shift=parsed.get('shift'),
            supervisor=parsed.get('supervisor'), data_snapshot=parsed,
            confidence=parsed.get('confidence', {}).get('overall'),
            api_used=api_used, cost_inr=cost, duration_ms=ms, image_file=img_name)

        return jsonify({
            'success':           True,
            'data':              parsed,
            'apiUsed':           api_used,
            'fallbackUsed':      fallback,
            'costINR':           cost,
            'durationMs':        ms,
            'machineValidation': machine_validation,
            'rowWarnings':       row_warnings,
            'hourWarnings':      hour_warnings,
            'oddCells':          odd_cells,
        })

    except Exception as e:
        audit_log.log_exception(Action.ERROR, e, request=request)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/records', methods=['GET'])
def get_records():
    try: return jsonify(db_get_all())
    except Exception as e: return jsonify({'error':str(e)}), 500

@app.route('/api/records', methods=['POST'])
def save_record():
    try:
        data = request.get_json(force=True)
        rid  = db_insert(data)
        audit_log.log_event(Action.RECORD_SAVED, Status.SUCCESS, details={'id':rid}, request=request,
            prod_date=data.get('date'), shift=data.get('shift'), supervisor=data.get('supervisor'))
        return jsonify({'success':True,'id':rid})
    except Exception as e:
        audit_log.log_exception(Action.ERROR, e, request=request)
        return jsonify({'success':False,'error':str(e)}), 500

@app.route('/api/records/<rid>', methods=['DELETE'])
def delete_record(rid):
    try:
        db_delete(rid)
        audit_log.log_event(Action.RECORD_DELETED, Status.SUCCESS, details={'id':rid}, request=request)
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'error':str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# NEW: Machine validation API endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/validate_machines', methods=['POST'])
def api_validate_machines():
    """
    POST body (option A): {"codes": ["030FF015", "030BC003"]}
    POST body (option B): {"ocr_data": { ...full OCR result... }}
    Returns active/inactive status from MCH_MASTER table.
    """
    try:
        body  = request.get_json(force=True) or {}
        codes = body.get('codes', [])
        if not codes and 'ocr_data' in body:
            codes = _extract_sap_codes_from_ocr(body['ocr_data'])
        if not codes:
            return jsonify({'success': False, 'error': 'No SAP codes provided or found'}), 400
        result = _mch_check(codes)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/validate_machine/<code>', methods=['GET'])
def api_validate_single(code):
    """Live single-code lookup — hits SQL directly every time."""
    try:
        code_clean = code.strip().upper()
        conn   = _sql_connect(_sql_db_for_codes([code_clean]))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MchType, CODE, DES, Mtype, Active FROM MCH_MASTER "
            "WHERE UPPER(LTRIM(RTRIM(CODE))) = ?",
            (code_clean,)
        )
        row = cursor.fetchone()
        conn.close()

        if row is None:
            return jsonify({
                'success': True, 'found': False,
                'code': code_clean, 'sql_available': True
            })
        return jsonify({
            'success':    True,
            'found':      True,
            'code':       code_clean,
            'sql_available': True,
            'active':     str(row[4] or '').strip().upper() == 'Y',
            'mch_type':   str(row[0] or '').strip(),
            'des':        str(row[2] or '').strip(),
            'mtype':      str(row[3] or '').strip(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/carding_preview/validate', methods=['POST', 'OPTIONS'])
def carding_preview_validate():
    """Called by preview page on load and after every cell edit — always live SQL."""
    if request.method == 'OPTIONS': return '', 200
    try:
        body     = request.get_json(force=True) or {}
        ocr_data = body.get('ocr_data', {})
        codes    = body.get('codes', [])
        if not codes and ocr_data:
            codes = _extract_sap_codes_from_ocr(ocr_data)
        if not codes:
            return jsonify({'success': True, 'machines': {},
                            'inactive_codes': [], 'not_found_codes': [], 'sql_available': True})
        result = _mch_check(codes)   # always hits SQL directly
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    
@app.route('/api/carding_preview/score', methods=['POST', 'OPTIONS'])
def carding_preview_score():
    """Recompute the confidence meter for the operator's current edits."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body     = request.get_json(force=True) or {}
        ocr_data = body.get('ocr_data') or {}
 
        ocr_data, row_warnings = normalize_materials(ocr_data)
        hour_warnings = validate_shift_hours(ocr_data)
        odd_cells     = find_unparseable_cells(ocr_data)
 
        codes = _extract_sap_codes_from_ocr(ocr_data)
        machine_validation = _mch_check(codes) if codes else {
            'sql_available': True, 'machines': {},
            'inactive_codes': [], 'not_found_codes': []}
 
        # The photo cannot change after upload, so carry the geometry score
        # forward from whatever the original OCR run measured.
        prev = ocr_data.get('confidence') or {}
        carried = {'after': {'drift_rows':
                             0.05 if prev.get('geometry', 0) >= 88 else 0.5}}
 
        conf = compute_confidence(
            ocr_data,
            machine_validation=machine_validation,
            hour_warnings=hour_warnings,
            odd_cells=odd_cells,
            row_warnings=row_warnings,
            prep_report=carried,
        )
        return jsonify({'success': True, 'confidence': conf,
                        'hourWarnings': hour_warnings,
                        'oddCells': odd_cells,
                        'rowWarnings': row_warnings,
                        'machineValidation': machine_validation}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/refresh_machine_cache', methods=['POST'])
def api_refresh_machine_cache():
    """Force re-fetch from SQL Server MCH_MASTER table."""
    try:
        ok    = _mch_load(force=True)
        with _mch_cache_lock:
            count = len(_mch_cache)
        return jsonify({'success': ok, 'machine_count': count,
                        'message': f'Refreshed — {count} machines loaded' if ok else 'SQL Server unreachable'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# Excel export
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/export/excel')
def export_excel():
    rows = db_get_all()
    wb   = openpyxl.Workbook()

    SAP_PREFIX = {'BC':'030BC','IC':'030IC','FC':'030FF','TC':'030TC','HW':'030HC'}
    SAP_DESC   = {'BC':'B.CRD','IC':'I.CRD','FC':'F.CRD','TC':'T.CRD','HW':'HW.CRD'}
    MACHINE_CLASS = {'BC':'1st Carding','IC':'2nd Carding','FC':'3rd Carding','TC':'4th Carding','HW':'5th Carding'}
    SHIFT_HRS  = {'1':8,'3/4':6,'1/2':4,'1/4':2}

    def parse_val(raw):
        raw = str(raw).strip()
        if not raw: return None, 0
        import re
        m = re.match(r'^(\d+)\((\d+/\d+)\)$', raw)
        if m: return m.group(1), SHIFT_HRS.get(m.group(2), 8)
        if re.match(r'^\d+$', raw): return raw, 8
        m2 = re.match(r'^\((\d+/\d+)\)$', raw)
        if m2: return None, SHIFT_HRS.get(m2.group(1), 8)
        return raw, 8

    def sap_code(sec, num_str):
        try: return f"{SAP_PREFIX[sec]}{int(num_str):03d}"
        except: return ''

    def sap_desc(sec, num_str):
        try: return f"{SAP_DESC[sec]}-{int(num_str):02d}"
        except: return ''

    def parse_cell_multi(raw):
        if not raw: return []
        result = []
        for p in str(raw).split(','):
            num, hrs = parse_val(p.strip())
            if num: result.append((num, hrs))
        return result

    def stop_sap_code(machine_text):
        import re
        text = str(machine_text).upper().strip()
        if   'F/CARD' in text or 'FL/CARD' in text or 'FINISHER' in text: sec = 'FC'
        elif 'B/CARD' in text or 'BREAKER' in text: sec = 'BC'
        elif 'I/CARD' in text or 'INTER' in text:   sec = 'IC'
        elif 'T/CARD' in text or 'TEASER' in text:  sec = 'TC'
        elif 'HW' in text: sec = 'HW'
        else: return '', ''
        m = re.search(r'NO\.?\s*(\d+)', text)
        if not m: return '', ''
        return sap_code(sec, m.group(1)), sap_desc(sec, m.group(1))

    def calc_down_hrs(start, end):
        import re
        def to_mins(t):
            t = str(t).strip().upper()
            m = re.match(r'(\d+):(\d+)\s*(AM|PM)?', t)
            if not m: return None
            h, mn = int(m.group(1)), int(m.group(2))
            ampm = m.group(3)
            if ampm == 'PM' and h != 12: h += 12
            if ampm == 'AM' and h == 12: h = 0
            return h*60 + mn
        s = to_mins(start); e = to_mins(end)
        if s is None or e is None: return ''
        diff = e - s
        if diff < 0: diff += 24*60
        return round(diff/60, 2)

    C = {'rec':'1F3864','bc':'833C11','ic':'375623','fc':'243F60','tc':'5C3317','stop':'404040',
         'rec_r':'D6E4F0','bc_r':'FFF2EC','ic_r':'F0F7EA','fc_r':'EAF2FA','tc_r':'FAF0E8',
         'mat_r':'F5F8FC','div':'C0C8D4','inactive':'FFD700','not_found':'FFB3B3'}

    def F(h): return PatternFill('solid', start_color=h, fgColor=h)
    def HF(c='FFFFFF'): return Font(bold=True, color=c, name='Arial', size=10)
    def DF(bold=False, color='1A1A1A'): return Font(bold=bold, color=color, name='Arial', size=10)
    CTR = Alignment(horizontal='center', vertical='center', wrap_text=True)
    LFT = Alignment(horizontal='left',   vertical='center', wrap_text=True)
    def TB(): s=Side(style='thin',color='AAAAAA'); return Border(left=s,right=s,top=s,bottom=s)
    def LB(): return Border(left=Side(style='medium',color='888888'),right=Side(style='thin',color='AAAAAA'),
                            top=Side(style='thin',color='AAAAAA'),bottom=Side(style='thin',color='AAAAAA'))

    # Load MCH_MASTER for Excel highlighting
    _mch_load()

    # ── Sheet 1: Carding Log ──────────────────────────────────────────────────
    ws1 = wb.active; ws1.title = 'Carding Log'
    spans = [('A1','A2','Date',C['rec']),('B1','B2','Shift',C['rec']),('C1','C2','Supervisor',C['rec']),
             ('D1','D2','Material',C['rec']),('E1','L1','Breaker Card',C['bc']),
             ('M1','Q1','Inter Card',C['ic']),('R1','AA1','Finisher Card',C['fc']),
             ('AB1','AC1','Teaser Card',C['tc']),('AD1','AD2','HW',C['tc']),('AE1','AE2','Stoppages',C['stop'])]
    for s,e,label,color in spans:
        if s!=e: ws1.merge_cells(f'{s}:{e}')
        c=ws1[s]; c.value=label; c.font=HF(); c.fill=F(color); c.alignment=CTR
    col=5
    for num,color in [(i,C['bc']) for i in range(1,9)]+[(i,C['ic']) for i in range(1,6)]+\
                     [(i,C['fc']) for i in range(1,11)]+[(i,C['tc']) for i in range(1,3)]:
        c=ws1.cell(row=2,column=col); c.value=f'M{num}'; c.font=HF(); c.fill=F(color); c.alignment=CTR; col+=1
    ws1.row_dimensions[1].height=22; ws1.row_dimensions[2].height=18
    SEC_LOG = {'BC':(5,8,C['bc_r']),'IC':(13,5,C['ic_r']),'FC':(18,10,C['fc_r']),'TC':(28,2,C['tc_r']),'HW':(30,1,C['tc_r'])}
    cur=3
    for ri,rec in enumerate(rows):
        mat_data=rec.get('materials',{})
        if isinstance(mat_data,str):
            try: mat_data=json.loads(mat_data)
            except: mat_data={}
        stops=rec.get('stoppages',[])
        if isinstance(stops,str):
            try: stops=json.loads(stops)
            except: stops=[]
        stop_str=' | '.join(f"{s.get('machine','')} {s.get('start_time','')}-{s.get('end_time','')}: {s.get('reason','')}"
                            for s in stops if s.get('machine') or s.get('reason')) or '—'
        sr=cur
        for mi,mk in enumerate(MATERIALS):
            r=cur; ms_data=mat_data.get(mk,{})
            if isinstance(ms_data,str):
                try: ms_data=json.loads(ms_data)
                except: ms_data={}
            if mi==0:
                for cn,val in [(1,rec.get('date','')), (2,rec.get('shift','')), (3,rec.get('supervisor',''))]:
                    c=ws1.cell(row=r,column=cn); c.value=val
                    c.font=DF(bold=True,color='1F3864'); c.fill=F(C['rec_r']); c.alignment=CTR; c.border=TB()
            c=ws1.cell(row=r,column=4); c.value=MATERIAL_LABELS[mk]
            c.font=DF(bold=True,color='333333'); c.fill=F(C['mat_r']); c.alignment=LFT; c.border=LB()
            for sk,(sc,cnt,rc) in SEC_LOG.items():
                sv=ms_data.get(sk,{})
                if isinstance(sv,str):
                    try: sv=json.loads(sv)
                    except: sv={}
                for i in range(1,cnt+1):
                    val=sv.get(f'M{i}','') if isinstance(sv,dict) else ''
                    cell_col = sc+i-1
                    c=ws1.cell(row=r,column=cell_col)
                    c.value = val or ''
                    # Determine SAP code for this cell value and check status
                    cell_bg = rc
                    cell_color = '333333' if val else 'CCCCCC'
                    if val and _mch_loaded:
                        for part in str(val).split(','):
                            m_num = _re.match(r'^(\d+)', part.strip())
                            if m_num:
                                code = f"{SAP_PREFIX[sk]}{int(m_num.group(1)):03d}"
                                with _mch_cache_lock:
                                    info = _mch_cache.get(code)
                                if info is None:
                                    cell_bg = 'FFB3B3'      # pink = not found
                                    cell_color = '8B0000'
                                    break
                                elif not info['active']:
                                    cell_bg = 'FFD700'      # gold = inactive
                                    cell_color = '7B4F00'
                                    break
                    c.font=DF(bold=bool(val),color=cell_color)
                    c.fill=F(cell_bg); c.alignment=CTR; c.border=TB()
            if mi==0:
                c=ws1.cell(row=r,column=31); c.value=stop_str
                c.font=DF(color='333333'); c.fill=F('F9F9F9'); c.alignment=LFT; c.border=TB()
            cur+=1
        er=cur-1
        if sr<er:
            for cn in [1,2,3,31]:
                ws1.merge_cells(start_row=sr,start_column=cn,end_row=er,end_column=cn)
                ws1.cell(row=sr,column=cn).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
        if ri<len(rows)-1:
            for cn in range(1,32): c=ws1.cell(row=cur,column=cn); c.fill=F(C['div']); c.value=''
            ws1.row_dimensions[cur].height=4; cur+=1
    widths1={'A':12,'B':7,'C':22,'D':14}
    for ch in 'EFGHIJKL': widths1[ch]=9
    for ch in 'MNOPQ': widths1[ch]=9
    for i in range(10): widths1[get_column_letter(18+i)]=9
    widths1['AB']=9; widths1['AC']=9; widths1['AD']=9; widths1['AE']=52
    for cl,w in widths1.items(): ws1.column_dimensions[cl].width=w
    for r in range(3,cur):
        if ws1.row_dimensions[r].height!=4: ws1.row_dimensions[r].height=20
    ws1.freeze_panes='E3'

    # ── Sheet 2: SAP Export ───────────────────────────────────────────────────
    ws2 = wb.create_sheet('SAP Export')
    sap_spans = [('A1','A2','Date',C['rec']),('B1','B2','Shift',C['rec']),
                 ('C1','C2','Supervisor',C['rec']),('D1','D2','Material',C['rec']),
                 ('E1','G1','1C — Breaker Card',C['bc']),('H1','J1','2C — Inter Card',C['ic']),
                 ('K1','M1','3C — Finisher Card',C['fc']),('N1','P1','CC — Teaser/HW',C['tc'])]
    for s,e,label,color in sap_spans:
        if s!=e: ws2.merge_cells(f'{s}:{e}')
        c=ws2[s]; c.value=label; c.font=HF(); c.fill=F(color); c.alignment=CTR
    for col_start, color in [(5,C['bc']),(8,C['ic']),(11,C['fc']),(14,C['tc'])]:
        for offset, lbl in enumerate(['SAP Code','Description','Hrs']):
            c=ws2.cell(row=2,column=col_start+offset)
            c.value=lbl; c.font=HF(); c.fill=F(color); c.alignment=CTR
    ws2.row_dimensions[1].height=22; ws2.row_dimensions[2].height=18
    SECS_SAP = [('BC',5),('IC',8),('FC',11),('TC',14),('HW',14)]
    SEC_SIZES_SAP = {'BC':8,'IC':5,'FC':10,'TC':2,'HW':1}
    cur2 = 3
    for ri, rec in enumerate(rows):
        mat_data=rec.get('materials',{})
        if isinstance(mat_data,str):
            try: mat_data=json.loads(mat_data)
            except: mat_data={}
        sap_rec = rec.get('sap_data', {})
        mat_rows = {}
        for mk in MATERIALS:
            mat_rows[mk] = {}
            sap_mk = sap_rec.get(mk, {}) if sap_rec else {}
            for sk in ['BC','IC','FC','TC','HW']:
                if sap_mk and sk in sap_mk and sap_mk[sk]:
                    entries = []
                    for e in sap_mk[sk]:
                        code = e.get('code','')
                        hrs  = e.get('hrs', 8)
                        try: d_str = sap_desc(sk, str(int(code[-3:])))
                        except: d_str = code
                        if code: entries.append((code, d_str, hrs))
                    mat_rows[mk][sk] = entries
                else:
                    ms_data = mat_data.get(mk,{})
                    if isinstance(ms_data,str):
                        try: ms_data=json.loads(ms_data)
                        except: ms_data={}
                    sv = ms_data.get(sk,{})
                    if isinstance(sv,str):
                        try: sv=json.loads(sv)
                        except: sv={}
                    entries = []
                    cnt = SEC_SIZES_SAP.get(sk,0)
                    for i in range(1,cnt+1):
                        raw = sv.get(f'M{i}','') if isinstance(sv,dict) else ''
                        if raw:
                            for num_str, hrs in parse_cell_multi(raw):
                                code = sap_code(sk, num_str)
                                desc = sap_desc(sk, num_str)
                                if code: entries.append((code, desc, hrs))
                    mat_rows[mk][sk] = entries
        for mk in MATERIALS:
            sec_entries = mat_rows[mk]
            max_rows = max((len(sec_entries.get(sk,[])) for sk in ['BC','IC','FC','TC','HW']), default=0)
            if max_rows == 0: max_rows = 1
            sr2 = cur2
            for row_i in range(max_rows):
                r = cur2
                if mk == MATERIALS[0] and row_i == 0:
                    for cn, val in [(1,rec.get('date','')), (2,rec.get('shift','')), (3,rec.get('supervisor',''))]:
                        c=ws2.cell(row=r,column=cn); c.value=val
                        c.font=DF(bold=True,color='1F3864'); c.fill=F(C['rec_r']); c.alignment=CTR; c.border=TB()
                if row_i == 0:
                    c=ws2.cell(row=r,column=4); c.value=MATERIAL_LABELS[mk]
                    c.font=DF(bold=True,color='333333'); c.fill=F(C['mat_r']); c.alignment=LFT; c.border=LB()
                else:
                    c=ws2.cell(row=r,column=4); c.value=''; c.fill=F(C['mat_r']); c.border=TB()
                for sk, col_start in SECS_SAP:
                    entries = sec_entries.get(sk,[])
                    if row_i < len(entries):
                        code, desc, hrs = entries[row_i]
                        # Status-based background
                        base_bg = {'BC':C['bc_r'],'IC':C['ic_r'],'FC':C['fc_r'],'TC':C['tc_r']}.get(sk,'FFFFFF')
                        if _mch_loaded:
                            with _mch_cache_lock:
                                info = _mch_cache.get(code.upper())
                            if info is None:     base_bg = 'FFB3B3'
                            elif not info['active']: base_bg = 'FFD700'
                        bg = base_bg
                        c=ws2.cell(row=r,column=col_start);   c.value=code; c.font=DF(bold=True,color='1D6F42'); c.fill=F(bg); c.alignment=CTR; c.border=TB()
                        c=ws2.cell(row=r,column=col_start+1); c.value=desc; c.font=DF(color='185FA5'); c.fill=F(bg); c.alignment=CTR; c.border=TB()
                        c=ws2.cell(row=r,column=col_start+2); c.value=hrs;  c.font=DF(bold=True,color='BA7517'); c.fill=F(bg); c.alignment=CTR; c.border=TB()
                    else:
                        bg = {'BC':C['bc_r'],'IC':C['ic_r'],'FC':C['fc_r'],'TC':C['tc_r']}.get(sk,'FFFFFF')
                        for col_off in range(3):
                            c=ws2.cell(row=r,column=col_start+col_off); c.value=''; c.fill=F(bg); c.alignment=CTR; c.border=TB()
                cur2 += 1
            if max_rows > 1:
                ws2.merge_cells(start_row=sr2,start_column=4,end_row=cur2-1,end_column=4)
                ws2.cell(row=sr2,column=4).alignment=Alignment(horizontal='left',vertical='center',wrap_text=True)
        rec_end = cur2 - 1
        rec_start_row = cur2 - sum(
            max(max((len(mat_rows[mk].get(sk,[])) for sk in ['BC','IC','FC','TC','HW']), default=0), 1)
            for mk in MATERIALS)
        for cn in [1,2,3]:
            ws2.merge_cells(start_row=rec_start_row,start_column=cn,end_row=rec_end,end_column=cn)
            ws2.cell(row=rec_start_row,column=cn).alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
        if ri < len(rows)-1:
            for cn in range(1,17): c=ws2.cell(row=cur2,column=cn); c.fill=F(C['div']); c.value=''
            ws2.row_dimensions[cur2].height=4; cur2+=1
    widths2={'A':12,'B':7,'C':22,'D':16,'E':12,'F':12,'G':8,'H':12,'I':12,'J':8,
             'K':12,'L':12,'M':8,'N':12,'O':12,'P':8,'Q':12,'R':12,'S':8}
    for cl,w in widths2.items(): ws2.column_dimensions[cl].width=w
    for r in range(3,cur2):
        if ws2.row_dimensions[r].height!=4: ws2.row_dimensions[r].height=20
    ws2.freeze_panes='E3'

    # ── Sheet 3: Stoppages ────────────────────────────────────────────────────
    ws3 = wb.create_sheet('Stoppages')
    stop_hdrs = ['Date','Shift','Supervisor','SAP Code','Description','Start Time','End Time','Down (hrs)','Reason']
    stop_colors = [C['rec'],C['rec'],C['rec'],C['stop'],C['stop'],C['stop'],C['stop'],C['stop'],C['stop']]
    for ci,(hdr,col) in enumerate(zip(stop_hdrs,stop_colors),1):
        c=ws3.cell(row=1,column=ci); c.value=hdr; c.font=HF(); c.fill=F(col); c.alignment=CTR; c.border=TB()
    ws3.row_dimensions[1].height=20
    cur3=2
    for rec in rows:
        stops=rec.get('stoppages',[])
        if isinstance(stops,str):
            try: stops=json.loads(stops)
            except: stops=[]
        if not stops: continue
        date=rec.get('date',''); shift=rec.get('shift',''); sup=rec.get('supervisor','')
        for s in stops:
            machine=s.get('machine',''); reason=s.get('reason','')
            start=s.get('start_time',''); end=s.get('end_time','')
            if not (machine or reason): continue
            code, desc = stop_sap_code(machine)
            down = calc_down_hrs(start, end)
            vals=[date,shift,sup,code,desc,start,end,down,reason]
            bgs=['D6E4F0','D6E4F0','D6E4F0','FFEBEE','FFEBEE','FFEBEE','FFEBEE','FFEBEE','FFEBEE']
            for ci,(val,bg) in enumerate(zip(vals,bgs),1):
                c=ws3.cell(row=cur3,column=ci); c.value=val
                bold = ci<=3 or ci==4
                col_  = '1F3864' if ci<=3 else ('C0392B' if ci==4 else '1A1A1A')
                c.font=DF(bold=bold,color=col_); c.fill=F(bg); c.alignment=(LFT if ci in [3,9] else CTR); c.border=TB()
            cur3+=1
    widths3={'A':12,'B':7,'C':22,'D':12,'E':12,'F':12,'G':12,'H':10,'I':45}
    for cl,w in widths3.items(): ws3.column_dimensions[cl].width=w
    for r in range(2,cur3): ws3.row_dimensions[r].height=20
    ws3.freeze_panes='A2'

    # ── Sheet 4: SAP Table ────────────────────────────────────────────────────
    ws4 = wb.create_sheet('SAP Table')
    SAP_TABLE_HDRS = ['Plnt','Dept','Machine Class','Basic Material','Date','Shift']
    MAX_WC = 25
    for i in range(1, MAX_WC+1): SAP_TABLE_HDRS.append(f'Work Ctr {i}')
    for ci, hdr in enumerate(SAP_TABLE_HDRS, 1):
        c = ws4.cell(row=1, column=ci)
        c.value = hdr; c.font = HF(); c.alignment = CTR
        c.fill = F('1F3864' if ci <= 4 else '243F60'); c.border = TB()
    ws4.row_dimensions[1].height = 20
    MACHINE_CLASS_MAP = {'BC':'1C','IC':'2C','FC':'3C','TC':'CC','HW':'CC'}
    MATERIAL_NAMES = {'hessian':'Ordinary Hessian','sacking_warp':'Sacking Warp',
                      'sacking_weft':'Sacking Weft','sale_yarn':'Sale Yarn',
                      '12lb_warp':'12 LBS WARP','15lb_weft':'13 Lb Heavy Warp & Weft'}
    PLANT = os.getenv('SAP_PLANT', 'MILL_A'); DEPT = '03'
    cur4 = 2
    for rec in rows:
        sap_rec  = rec.get('sap_data', {})
        mat_data = rec.get('materials', {})
        if isinstance(mat_data, str):
            try: mat_data = json.loads(mat_data)
            except: mat_data = {}
        for mk in MATERIALS:
            sap_mk   = sap_rec.get(mk, {}) if sap_rec else {}
            mat_name = MATERIAL_NAMES.get(mk, mk)
            for sec_key, mc_label in MACHINE_CLASS_MAP.items():
                codes = []
                if sap_mk and sec_key in sap_mk and sap_mk[sec_key]:
                    codes = [e.get('code','') for e in sap_mk[sec_key] if e.get('code')]
                else:
                    ms_data = mat_data.get(mk, {})
                    if isinstance(ms_data, str):
                        try: ms_data = json.loads(ms_data)
                        except: ms_data = {}
                    sv = ms_data.get(sec_key, {})
                    if isinstance(sv, str):
                        try: sv = json.loads(sv)
                        except: sv = {}
                    cnt = SEC_SIZES_SAP.get(sec_key, 0)
                    for i in range(1, cnt+1):
                        raw = sv.get(f'M{i}', '') if isinstance(sv, dict) else ''
                        if raw:
                            for num_str, hrs in parse_cell_multi(raw):
                                code = sap_code(sec_key, num_str)
                                if code: codes.append(code)
                if not codes: continue
                rec_date = rec.get('date','')
                try:
                    from datetime import datetime as _dt
                    for _fmt in ('%d-%m-%Y','%Y-%m-%d','%d/%m/%Y','%d.%m.%Y'):
                        try: rec_date = _dt.strptime(rec_date,_fmt).strftime('%d.%m.%Y'); break
                        except: pass
                except: pass
                rec_shift = str(rec.get('shift','')).upper()
                row_vals = [PLANT, DEPT, mc_label, mat_name, rec_date, rec_shift]
                for i in range(MAX_WC): row_vals.append(codes[i] if i < len(codes) else '')
                while len(row_vals) > 4 and row_vals[-1] == '': row_vals.pop()
                while len(row_vals) < 4 + MAX_WC: row_vals.append('')
                sec_bg = {'BC':C['bc_r'],'IC':C['ic_r'],'FC':C['fc_r'],'TC':C['tc_r'],'HW':C['tc_r']}.get(sec_key,'FFFFFF')
                for ci, val in enumerate(row_vals, 1):
                    c = ws4.cell(row=cur4, column=ci); c.value = val
                    if ci == 1: c.font=DF(bold=True,color='1F3864'); c.fill=F(C['rec_r'])
                    elif ci == 2: c.font=DF(bold=True,color='1F3864'); c.fill=F(C['rec_r'])
                    elif ci == 3: c.font=DF(bold=True,color='333333'); c.fill=F(sec_bg)
                    elif ci == 4: c.font=DF(bold=True,color='333333'); c.fill=F(C['mat_r']); c.alignment=LFT
                    else:
                        # SAP Table: highlight inactive/not-found codes
                        col_bg = sec_bg
                        col_fg = '1D6F42' if val else 'CCCCCC'
                        if val and _mch_loaded:
                            with _mch_cache_lock:
                                info = _mch_cache.get(val.upper())
                            if info is None:        col_bg='FFB3B3'; col_fg='8B0000'
                            elif not info['active']: col_bg='FFD700'; col_fg='7B4F00'
                        c.font=DF(bold=bool(val),color=col_fg); c.fill=F(col_bg); c.alignment=CTR
                    if ci != 4: c.alignment=CTR
                    c.border=TB()
                ws4.row_dimensions[cur4].height = 18; cur4 += 1
    ws4.column_dimensions['A'].width=8; ws4.column_dimensions['B'].width=6
    ws4.column_dimensions['C'].width=16; ws4.column_dimensions['D'].width=22
    ws4.column_dimensions['E'].width=14; ws4.column_dimensions['F'].width=8
    for i in range(1, MAX_WC+1): ws4.column_dimensions[get_column_letter(6+i)].width=11
    ws4.freeze_panes='G2'

    # ── Sheet 5: Machine Status (NEW) ─────────────────────────────────────────
    ws5 = wb.create_sheet('Machine Status')
    ms_hdrs = ['SAP Code','Description','Machine Type','Mtype','Active','Status']
    ms_colors = ['1F3864','1F3864','1F3864','1F3864','1F3864','1F3864']
    for ci, hdr in enumerate(ms_hdrs, 1):
        c=ws5.cell(row=1,column=ci); c.value=hdr; c.font=HF(); c.fill=F('1F3864'); c.alignment=CTR; c.border=TB()
    ws5.row_dimensions[1].height=20
    # Collect all unique codes from all records
    all_codes = set()
    for rec in rows:
        mat_data = rec.get('materials', {})
        if isinstance(mat_data, str):
            try: mat_data = json.loads(mat_data)
            except: mat_data = {}
        for mk, ms in mat_data.items():
            if not isinstance(ms, dict): continue
            for sec, slots in ms.items():
                if sec not in SAP_PREFIX or not isinstance(slots, dict): continue
                cnt = SEC_SIZES_SAP.get(sec, 0)
                for i in range(1, cnt+1):
                    raw = slots.get(f'M{i}', '')
                    if raw:
                        for part in str(raw).split(','):
                            m_n = _re.match(r'^(\d+)', part.strip())
                            if m_n: all_codes.add(f"{SAP_PREFIX[sec]}{int(m_n.group(1)):03d}")
    cur5 = 2
    for code in sorted(all_codes):
        with _mch_cache_lock:
            info = _mch_cache.get(code.upper()) if _mch_loaded else None
        if info is None:
            row_vals = [code, '—', '—', '—', '—', '❓ NOT FOUND IN MCH_MASTER']
            row_bgs  = ['FFB3B3','FFB3B3','FFB3B3','FFB3B3','FFB3B3','FFB3B3']
            txt_clr  = '8B0000'
        elif not info['active']:
            row_vals = [code, info['des'], info['mch_type'], info['mtype'], 'N', '⚠️ INACTIVE']
            row_bgs  = ['FFD700','FFD700','FFD700','FFD700','FFD700','FFD700']
            txt_clr  = '7B4F00'
        else:
            row_vals = [code, info['des'], info['mch_type'], info['mtype'], 'Y', '✅ ACTIVE']
            row_bgs  = ['E8F5E9','E8F5E9','E8F5E9','E8F5E9','E8F5E9','E8F5E9']
            txt_clr  = '1B5E20'
        for ci, (val, bg) in enumerate(zip(row_vals, row_bgs), 1):
            c=ws5.cell(row=cur5,column=ci); c.value=val
            c.font=DF(bold=(ci==1 or ci==6),color=txt_clr); c.fill=F(bg); c.alignment=CTR; c.border=TB()
        ws5.row_dimensions[cur5].height=18; cur5+=1
    # Legend row
    cur5 += 1
    ws5.cell(row=cur5,column=1).value='LEGEND:'; ws5.cell(row=cur5,column=1).font=HF('333333')
    for ci,( label, bg) in enumerate([('✅ Active — OK','E8F5E9'),('⚠️ Inactive (Active=N)','FFD700'),('❓ Not found in MCH_MASTER','FFB3B3')],2):
        c=ws5.cell(row=cur5,column=ci); c.value=label; c.fill=F(bg)
        c.font=DF(bold=True,color='333333'); c.alignment=LFT; c.border=TB()
    ws5.column_dimensions['A'].width=14; ws5.column_dimensions['B'].width=14
    ws5.column_dimensions['C'].width=28; ws5.column_dimensions['D'].width=22
    ws5.column_dimensions['E'].width=10; ws5.column_dimensions['F'].width=22
    ws5.freeze_panes='A2'

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    fname=f"Carding_Log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    audit_log.log_event(Action.EXCEL_EXPORTED, Status.SUCCESS, details={'rows':len(rows)}, request=request)
    return send_file(buf, download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True)

@app.route('/api/push_to_sap', methods=['POST'])
def push_to_sap():
    import threading
    try:
        body    = request.get_json(force=True)
        records = body.get('records', [])
        if not records:
            return jsonify({'success': False, 'error': 'No records provided'}), 400
        SAP_PREFIX = _sap_prefix_map('MILL_A')   # default; overridden per-record below
        MATERIAL_NAMES = {'hessian':'Ordinary Hessian','sacking_warp':'Sacking Warp',
                          'sacking_weft':'Sacking Weft','sale_yarn':'Sale Yarn',
                          '12lb_warp':'12 LBS WARP','15lb_weft':'13 Lb Heavy Warp & Weft'}
        PLANT_DEFAULT = os.getenv('SAP_PLANT', 'MILL_A')
        DEPT = '03'
        SEC_SIZES = {'BC':8,'IC':5,'FC':10,'TC':2,'HW':1}
        def parse_codes(raw, sec):
            codes = []
            for p in str(raw).split(','):
                c = _code_from_token(p, sec, rec_plant)
                if c: codes.append(c)
            return codes
        sap_rows = []
        for rec in records:
            mat_data = rec.get('materials', {})
            if isinstance(mat_data, str):
                try: mat_data = json.loads(mat_data)
                except: mat_data = {}
            if isinstance(mat_data, dict) and 'written' in mat_data:
                mat_data = mat_data.get('written', {})
            rec_date = str(rec.get('date', '')).strip()
            try:
                from datetime import datetime as _dt
                for _fmt in ('%d-%m-%Y','%Y-%m-%d','%d/%m/%Y','%d.%m.%Y'):
                    try: rec_date = _dt.strptime(rec_date, _fmt).strftime('%d.%m.%Y'); break
                    except: pass
            except: pass
            rec_shift = str(rec.get('shift', '')).upper()
            rec_plant = str(rec.get('plant', PLANT_DEFAULT)).upper() or PLANT_DEFAULT
            SAP_PREFIX = _sap_prefix_map(rec_plant, rec.get('shed'))
            for mk in MATERIALS:
                mat_name = MATERIAL_NAMES.get(mk, mk)
                ms = mat_data.get(mk, {})
                if isinstance(ms, str):
                    try: ms = json.loads(ms)
                    except: ms = {}
                def get_codes(sec_key, _ms=ms):
                    sv = _ms.get(sec_key, {})
                    if isinstance(sv, str):
                        try: sv = json.loads(sv)
                        except: sv = {}
                    codes = []
                    for i in range(1, SEC_SIZES.get(sec_key, 0)+1):
                        raw = sv.get(f'M{i}', '') if isinstance(sv, dict) else ''
                        if raw: codes.extend(parse_codes(raw, sec_key))
                    return codes
                for sec_key, mc in [('BC','1C'),('IC','2C'),('FC','3C')]:
                    codes = get_codes(sec_key)
                    if codes:
                        sap_rows.append({'plnt':rec_plant,'dept':DEPT,'mach_cl':mc,
                                         'material':mat_name,'date':rec_date,'shift':rec_shift,'work_ctrs':codes})
                cc = get_codes('TC') + get_codes('HW')
                if cc:
                    sap_rows.append({'plnt':rec_plant,'dept':DEPT,'mach_cl':'CC',
                                     'material':mat_name,'date':rec_date,'shift':rec_shift,'work_ctrs':cc})
        if not sap_rows:
            return jsonify({'success': False, 'error': 'No SAP rows generated — check record materials'}), 400
        tmp_dir   = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(tmp_dir, 'sap_data.json')
        log_path  = os.path.join(tmp_dir, 'sap_push_log.txt')
        with open(json_path, 'w') as f: json.dump(sap_rows, f, indent=2)
        script_path = os.path.join(tmp_dir, 'sap_script.py')
        if not os.path.exists(script_path):
            return jsonify({'success': False, 'error': 'sap_script.py not found'}), 500
        def run_sap():
            try:
                with open(log_path, 'w') as log_f:
                    subprocess.run([sys.executable, script_path, '--json', json_path, '--auto'],
                                   stdout=log_f, stderr=log_f, timeout=600, cwd=tmp_dir)
            except Exception as e:
                with open(log_path, 'a') as log_f: log_f.write(f'\n[THREAD ERROR] {e}')
        t = threading.Thread(target=run_sap, daemon=True); t.start(); t.join(timeout=90)
        output = ''
        try:
            with open(log_path, 'r') as f: output = f.read()
        except: pass
        success_count = output.count('] OK:')
        failed_count  = output.count('  [ERROR] Row')
        sap_conn_error = '[ERROR] Cannot connect to SAP' in output
        no_data_error  = '[ERROR] No rows to enter' in output or '[ERROR] No data source' in output
        thread_error   = '[THREAD ERROR]' in output
        if sap_conn_error: error_detail = 'SAP GUI not connected. Open SAP GUI, log in, and enable scripting.'
        elif no_data_error: error_detail = 'No data rows generated — check OCR extracted machine data correctly.'
        elif thread_error:
            m = _re.search(r'\[THREAD ERROR\] (.+)', output)
            error_detail = m.group(1) if m else 'Script launch failed.'
        else: error_detail = None
        _first = records[0] if records else {}
        audit_log.log_event(Action.SAP_PUSH,
            Status.SUCCESS if (failed_count==0 and not sap_conn_error) else Status.WARNING,
            details={'success':success_count,'failed':failed_count,'rows':len(sap_rows)},
            request=request, prod_date=_first.get('date'), shift=_first.get('shift'))
        if success_count > 0 and not sap_conn_error:
            mdb = get_mongo()
            from bson import ObjectId
            for rec in records:
                try:
                    rid = rec.get('id') or rec.get('_id')
                    if rid and mdb is not None:
                        mdb.records.update_one({'_id': ObjectId(str(rid))},
                            {'$set': {'sap_pushed':True,'sap_pushed_at':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}})
                except: pass
        return jsonify({'success':not sap_conn_error,'success_count':success_count,'failed_count':failed_count,
                        'message':error_detail if error_detail else f"SAP push: {success_count} rows entered, {failed_count} failed",
                        'output':output[-3000:]})
    except Exception as e:
        audit_log.log_exception(Action.SAP_PUSH, e, request=request)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sap_log')
def sap_log():
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sap_push_log.txt')
    if not os.path.exists(log_path):
        return jsonify({'exists': False, 'content': 'No log file found yet. Run a SAP push first.'})
    try:
        with open(log_path, 'r') as f: content_log = f.read()
        return jsonify({'exists': True, 'content': content_log, 'size': len(content_log)})
    except Exception as e:
        return jsonify({'exists': False, 'content': f'Error reading log: {e}'})

@app.route('/api/diagnose')
def diagnose():
    env_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),'.env')
    mdb=get_mongo()
    report={
        'env_file_exists':os.path.exists(env_path),
        'database':'MongoDB @ '+MONGO_URI if mdb is not None else 'SQLite (MongoDB not running)',
        'claude_key_loaded':bool(CLAUDE_API_KEY and CLAUDE_API_KEY!='YOUR_CLAUDE_KEY_HERE'),
        'claude_key_prefix':CLAUDE_API_KEY[:15]+'...' if CLAUDE_API_KEY!='YOUR_CLAUDE_KEY_HERE' else '(not set)',
        'gemini_key_loaded':bool(GEMINI_API_KEY and GEMINI_API_KEY!='YOUR_GEMINI_KEY_HERE'),
        'claude_api_test':None,'gemini_api_test':None,
        # ── SQL Server status ─────────────────────────────────────────────────
        'sql_server': {
            'server':        SQL_SERVER,
            'database':      SQL_DATABASE,
            'connected':     _mch_loaded,
            'machine_count': len(_mch_cache) if _mch_loaded else 0,
        }
    }
    if report['claude_key_loaded']:
        try:
            r=http_req.post('https://api.anthropic.com/v1/messages',
                headers={'Content-Type':'application/json','x-api-key':CLAUDE_API_KEY,'anthropic-version':'2023-06-01'},
                json={'model':'claude-sonnet-4-6','max_tokens':10,'messages':[{'role':'user','content':'Say OK'}]},timeout=15)
            report['claude_api_test']='PASS' if r.status_code==200 else f'FAIL HTTP {r.status_code}: {r.text[:100]}'
        except Exception as e: report['claude_api_test']=f'FAIL: {e}'
    if report['gemini_key_loaded']:
        try:
            r=http_req.post(f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}',
                json={'contents':[{'parts':[{'text':'Say OK'}]}],'generationConfig':{'maxOutputTokens':10}},timeout=15)
            report['gemini_api_test']='PASS' if r.status_code==200 else f'FAIL HTTP {r.status_code}: {r.text[:100]}'
        except Exception as e: report['gemini_api_test']=f'FAIL: {e}'
    return jsonify(report)

@app.route('/api/health')
def health():
    mdb=get_mongo()
    return jsonify({'status':'ok','database':'mongodb' if mdb is not None else 'sqlite',
                    'claude_configured':CLAUDE_API_KEY!='YOUR_CLAUDE_KEY_HERE',
                    'gemini_configured':GEMINI_API_KEY!='YOUR_GEMINI_KEY_HERE',
                    'sql_connected':_mch_loaded,'sql_machine_count':len(_mch_cache)})

@app.route('/api/audit/auth', methods=['POST'])
def audit_auth():
    body=request.get_json(force=True)
    if check_password_hash(AUDIT_HASH, body.get('password','')):
        session['audit_ok']=True
        audit_log.log_event(Action.AUDIT_VIEWED, Status.SUCCESS, details={'access':'granted'}, request=request)
        return jsonify({'success':True})
    audit_log.log_event(Action.AUDIT_AUTH_FAIL, Status.FAILED, details={'access':'denied'}, request=request)
    return jsonify({'success':False,'error':'Wrong password'}), 401

@app.route('/api/audit/logs', methods=['POST'])
def get_audit_logs():
    if not session.get('audit_ok'): return jsonify({'success':False,'error':'Not authenticated'}), 403
    body=request.get_json(force=True)
    logs,total=audit_log.query_logs(get_mongo(),body.get('filters',{}),body.get('limit',200),body.get('offset',0))
    return jsonify({'success':True,'logs':logs,'total':total})

@app.route('/api/audit/summary', methods=['GET'])
def audit_summary():
    if not session.get('audit_ok'): return jsonify({'success':False,'error':'Not authenticated'}), 403
    return jsonify({'success':True,'summary':audit_log.get_summary(get_mongo(),request.args.get('period','today'))})

@app.route('/api/audit/filters', methods=['GET'])
def audit_filters():
    if not session.get('audit_ok'): return jsonify({'success':False,'error':'Not authenticated'}), 403
    return jsonify({'actions':audit_log.distinct_values(get_mongo(),'action'),
                    'shifts':audit_log.distinct_values(get_mongo(),'shift'),
                    'dates':audit_log.distinct_values(get_mongo(),'prod_date'),
                    'apis':audit_log.distinct_values(get_mongo(),'api_used')})

@app.route('/api/audit/logout', methods=['POST'])
def audit_logout():
    session.pop('audit_ok',None); return jsonify({'success':True})

# ══════════════════════════════════════════════════════════════════════════════
# CARDING PREVIEW (WhatsApp bot integration)
# ══════════════════════════════════════════════════════════════════════════════
import uuid
import datetime as _dt_mod

_carding_sessions      = {}
_carding_sessions_lock = threading.Lock()
CARDING_SESSION_TTL    = 2 * 60 * 60

def _carding_session_create(ocr_data, jid):
    sid = str(uuid.uuid4()).replace('-', '')
    with _carding_sessions_lock:
        _carding_sessions[sid] = {
            'ocr_data': ocr_data, 'jid': jid,
            'created_at': _dt_mod.datetime.utcnow().isoformat(),
            'confirmed': False, 'rejected': False,
        }
    def _expire():
        import time; time.sleep(CARDING_SESSION_TTL)
        with _carding_sessions_lock: _carding_sessions.pop(sid, None)
    threading.Thread(target=_expire, daemon=True).start()
    return sid

def _carding_session_get(sid):
    with _carding_sessions_lock: return _carding_sessions.get(sid)

@app.route('/carding_preview', methods=['GET'])
def carding_preview_page():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'carding_preview.html')
    if not os.path.exists(html_path): return 'carding_preview.html not found', 404
    with open(html_path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/api/carding_preview/create', methods=['POST', 'OPTIONS'])
def carding_preview_create():
    if request.method == 'OPTIONS': return '', 200
    body = request.get_json(force=True) or {}
    ocr_data = body.get('ocr_data', {})
    jid      = body.get('jid', '')
    if isinstance(ocr_data, dict):
        ocr_data['plant'] = str(ocr_data.get('plant') or 'MILL_A').upper()
        ocr_data.pop('_machine_validation', None)  # never bake stale validation
    sid = _carding_session_create(ocr_data, jid)
    print(f'[carding_preview] session created: {sid}')
    return jsonify({'session_id': sid}), 200

@app.route('/api/carding_preview/data', methods=['GET', 'OPTIONS'])
def carding_preview_data():
    if request.method == 'OPTIONS': return '', 200
    sid     = request.args.get('s', '')
    sess    = _carding_session_get(sid)
    if not sess: return jsonify({'error': 'Session not found or expired'}), 404
    return jsonify({'ocr_data': sess['ocr_data']}), 200

@app.route('/api/carding_preview/confirm', methods=['POST', 'OPTIONS'])
def carding_preview_confirm():
    if request.method == 'OPTIONS': return '', 200
    body     = request.get_json(force=True) or {}
    sid      = body.get('session_id', '')
    ocr_data = body.get('ocr_data', None)
    with _carding_sessions_lock:
        sess = _carding_sessions.get(sid)
        if not sess: return jsonify({'ok': False, 'error': 'Session not found or expired'}), 404
        if ocr_data:
            if isinstance(ocr_data, dict): ocr_data['plant'] = str(ocr_data.get('plant') or 'MILL_A').upper()
            sess['ocr_data'] = ocr_data
        sess['confirmed'] = True
    data_to_save = sess['ocr_data']
    try:
        saved_id = db_insert(data_to_save)
        print(f'[carding_preview] ✅ Saved to DB id={saved_id}')
    except Exception as e:
        print(f'[carding_preview] ❌ DB save error: {e}')
        return jsonify({'ok': False, 'error': f'DB save failed: {e}'}), 500
    try:
        import json as _json, subprocess as _sub, sys as _sys
        SAP_PREFIX     = _sap_prefix_map(data_to_save.get('plant'), data_to_save.get('shed'))
        MATERIAL_NAMES = {'hessian':'Ordinary Hessian','sacking_warp':'Sacking Warp',
                          'sacking_weft':'Sacking Weft','sale_yarn':'Sale Yarn',
                          '12lb_warp':'12 LBS WARP','15lb_weft':'13 Lb Heavy Warp & Weft'}
        PLANT = str(data_to_save.get('plant') or 'MILL_A').upper(); DEPT = '03'
        SEC_SIZES = {'BC':8,'IC':5,'FC':10,'TC':2,'HW':1}
        def _parse_codes(raw, sec):
            codes = []
            for p in str(raw).split(','):
                c = _code_from_token(p, sec, PLANT)
                if c: codes.append(c)
            return codes
        mat_data  = data_to_save.get('materials', {})
        rec_date  = str(data_to_save.get('date', '')).strip()
        try:
            from datetime import datetime as _dt2
            for _fmt in ('%d-%m-%Y','%Y-%m-%d','%d/%m/%Y','%d.%m.%Y'):
                try: rec_date = _dt2.strptime(rec_date, _fmt).strftime('%d.%m.%Y'); break
                except: pass
        except: pass
        rec_shift = str(data_to_save.get('shift', '')).upper()
        sap_rows  = []
        for mk in MATERIALS:
            mat_name = MATERIAL_NAMES.get(mk, mk)
            ms = mat_data.get(mk, {})
            if isinstance(ms, str):
                try: ms = _json.loads(ms)
                except: ms = {}
            def _get_codes(sec_key, _ms=ms):
                sv = _ms.get(sec_key, {})
                if isinstance(sv, str):
                    try: sv = _json.loads(sv)
                    except: sv = {}
                codes = []
                for i in range(1, SEC_SIZES.get(sec_key, 0)+1):
                    raw = sv.get(f'M{i}','') if isinstance(sv,dict) else ''
                    if raw: codes.extend(_parse_codes(raw, sec_key))
                return codes
            for sec_key, mc in [('BC','1C'),('IC','2C'),('FC','3C')]:
                codes = _get_codes(sec_key)
                if codes:
                    sap_rows.append({'plnt':PLANT,'dept':DEPT,'mach_cl':mc,'material':mat_name,
                                     'date':rec_date,'shift':rec_shift,'work_ctrs':codes})
            cc = _get_codes('TC') + _get_codes('HW')
            if cc:
                sap_rows.append({'plnt':PLANT,'dept':DEPT,'mach_cl':'CC','material':mat_name,
                                 'date':rec_date,'shift':rec_shift,'work_ctrs':cc})
        if sap_rows:
            base_dir  = os.path.dirname(os.path.abspath(__file__))
            json_path = os.path.join(base_dir, 'sap_data.json')
            sap_script = os.path.join(base_dir, 'sap_script.py')
            with open(json_path, 'w') as f: _json.dump(sap_rows, f, indent=2)
            print(f'[carding_preview] SAP rows: {len(sap_rows)}')
            if os.path.exists(sap_script):
                _sub.Popen([_sys.executable, sap_script, '--json', json_path, '--auto'])
                print('[carding_preview] ✅ SAP script launched')
            else:
                print('[carding_preview] ⚠️ sap_script.py not found')
        else:
            print('[carding_preview] ⚠️ No SAP rows generated')
    except Exception as e:
        print(f'[carding_preview] ❌ SAP launch error: {e}')
        import traceback; traceback.print_exc()
    return jsonify({'ok': True, 'saved_id': str(saved_id)}), 200

@app.route('/api/carding_preview/reject', methods=['POST', 'OPTIONS'])
def carding_preview_reject():
    if request.method == 'OPTIONS': return '', 200
    body = request.get_json(force=True) or {}
    sid  = body.get('session_id', '')
    with _carding_sessions_lock:
        sess = _carding_sessions.get(sid)
        if not sess: return jsonify({'ok': False, 'error': 'Session not found'}), 404
        sess['rejected'] = True
    print(f'[carding_preview] ❌ REJECTED sid={sid}')
    return jsonify({'ok': True}), 200

@app.route('/api/carding_preview/status', methods=['GET', 'OPTIONS'])
def carding_preview_status():
    if request.method == 'OPTIONS': return '', 200
    sid  = request.args.get('s', '')
    sess = _carding_session_get(sid)
    if not sess:               return jsonify({'status': 'expired'}), 200
    if sess['confirmed']:      return jsonify({'status': 'confirmed'}), 200
    if sess['rejected']:       return jsonify({'status': 'rejected'}), 200
    return jsonify({'status': 'pending'}), 200

if __name__ == '__main__':
    env_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),'.env')
    mdb=get_mongo()
    print('='*60)
    print('  Carding OCR System v3  (+ SQL Server Machine Validator)')
    print('='*60)
    print(f"  .env      : {'FOUND' if os.path.exists(env_path) else 'NOT FOUND'}")
    print(f"  Database  : {'MongoDB @ '+MONGO_URI if mdb is not None else 'SQLite fallback'}")
    print(f"  Claude    : {'OK' if CLAUDE_API_KEY!='YOUR_CLAUDE_KEY_HERE' else 'NOT SET'}")
    print(f"  Gemini    : {'OK' if GEMINI_API_KEY!='YOUR_GEMINI_KEY_HERE' else 'NOT SET'}")
    print(f"  SQL Server: {SQL_SERVER} / {SQL_DATABASE}")
    print(f"  MCH_MASTER Cache: {'✅ '+str(len(_mch_cache))+' machines' if _mch_loaded else '⏳ loading...'}")
    print('='*60)
    print('  Local  : http://localhost:5000')
    print('  Network: http://<your-laptop-ip>:5001')
    print('  Diag   : http://localhost:5001/api/diagnose')
    print('='*60)
    app.run(debug=False, host='0.0.0.0', port=5001)
