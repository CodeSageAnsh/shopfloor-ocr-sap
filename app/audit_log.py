"""
audit_log.py — supports MongoDB (primary) and SQLite (fallback)
Never raises — audit must not crash the main app.
"""
import os, json, datetime as dt, traceback, sqlite3

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'carding_data.db')

class Action:
    LOGIN='LOGIN'; LOGOUT='LOGOUT'; AUDIT_VIEWED='AUDIT_VIEWED'; AUDIT_AUTH_FAIL='AUDIT_AUTH_FAIL'
    IMAGE_UPLOADED='IMAGE_UPLOADED'; OCR_STARTED='OCR_STARTED'; OCR_SUCCESS='OCR_SUCCESS'
    OCR_FAILED='OCR_FAILED'; OCR_FALLBACK='OCR_FALLBACK'
    RECORD_VERIFIED='RECORD_VERIFIED'; RECORD_SAVED='RECORD_SAVED'
    RECORD_EDITED='RECORD_EDITED'; RECORD_DELETED='RECORD_DELETED'
    EXCEL_EXPORTED='EXCEL_EXPORTED'; SAP_PUSH='SAP_PUSH'; ERROR='ERROR'

class Status:
    SUCCESS='success'; FAILED='failed'; WARNING='warning'

def _sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_audit_table(mdb=None):
    try:
        if mdb is not None:
            mdb.audit_logs.create_index('timestamp')
            mdb.audit_logs.create_index('action')
            mdb.audit_logs.create_index('prod_date')
            print('[AUDIT] MongoDB audit collection ready')
        else:
            conn = _sqlite()
            conn.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime("now","localtime")),
                user TEXT, host TEXT, ip TEXT, action TEXT NOT NULL,
                status TEXT DEFAULT "success", details TEXT,
                prod_date TEXT, shift TEXT, supervisor TEXT,
                data_snapshot TEXT, confidence REAL, api_used TEXT,
                cost_inr REAL, duration_ms INTEGER, image_file TEXT,
                before_data TEXT, after_data TEXT
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ts ON audit_logs(timestamp)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ac ON audit_logs(action)')
            conn.commit(); conn.close()
            print('[AUDIT] SQLite audit table ready')
    except Exception as e:
        print(f'[AUDIT INIT ERROR] {e}')

def _identity(request):
    user,host,ip='operator','unknown','unknown'
    if request:
        try:
            user = request.headers.get('X-User') or os.getenv('USERNAME') or os.getenv('USER') or 'operator'
            host = request.headers.get('X-Host') or os.getenv('COMPUTERNAME') or os.getenv('HOSTNAME') or 'unknown'
            ip   = request.headers.get('X-Forwarded-For') or getattr(request,'remote_addr','unknown')
        except: pass
    return user, host, ip

def log_event(action, status=Status.SUCCESS, details=None, request=None, user=None, host=None, ip=None,
              prod_date=None, shift=None, supervisor=None, data_snapshot=None, confidence=None,
              api_used=None, cost_inr=None, duration_ms=None, image_file=None, before=None, after=None,
              _mdb=None):
    try:
        if not (user and host and ip):
            u,h,i = _identity(request)
            user=user or u; host=host or h; ip=ip or i
        ts = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f'[AUDIT] {action} | {status} | user={user} | date={prod_date} | shift={shift}')

        # Try to get mdb from caller via module-level trick
        mdb = _mdb
        if mdb is None:
            try:
                import app as _app
                mdb = _app.get_mongo()
            except: pass

        if mdb is not None:
            doc = {'timestamp':ts,'user':user,'host':host,'ip':ip,'action':action,'status':status,
                   'details':details,'prod_date':prod_date,'shift':shift,'supervisor':supervisor,
                   'data_snapshot':data_snapshot,'confidence':confidence,'api_used':api_used,
                   'cost_inr':cost_inr,'duration_ms':duration_ms,'image_file':image_file,
                   'before_data':before,'after_data':after}
            mdb.audit_logs.insert_one({k:v for k,v in doc.items() if v is not None})
        else:
            conn = _sqlite()
            conn.execute('''INSERT INTO audit_logs
                (user,host,ip,action,status,details,prod_date,shift,supervisor,data_snapshot,
                 confidence,api_used,cost_inr,duration_ms,image_file,before_data,after_data)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                user,host,ip,action,status,
                json.dumps(details) if details else None,
                prod_date,shift,supervisor,
                json.dumps(data_snapshot) if data_snapshot else None,
                confidence,api_used,cost_inr,duration_ms,image_file,
                json.dumps(before) if before else None,
                json.dumps(after) if after else None
            ))
            conn.commit(); conn.close()
    except Exception as e:
        print(f'[AUDIT ERROR] {action}: {e}')

def log_exception(action, exc, request=None, **kwargs):
    log_event(action=action, status=Status.FAILED,
              details={'error':str(exc),'traceback':traceback.format_exc()[-2000:]},
              request=request, **kwargs)

def query_logs(mdb=None, filters=None, limit=200, offset=0):
    filters = filters or {}
    try:
        if mdb is not None:
            q = {}
            if filters.get('from_date'): q['timestamp'] = {'$gte': filters['from_date']}
            if filters.get('to_date'):   q.setdefault('timestamp',{})['$lte'] = filters['to_date']+' 23:59:59'
            if filters.get('action') and filters['action']!='ALL': q['action'] = filters['action']
            if filters.get('status') and filters['status']!='ALL': q['status'] = filters['status']
            if filters.get('shift')  and filters['shift'] !='ALL': q['shift']  = filters['shift']
            if filters.get('prod_date'): q['prod_date'] = filters['prod_date']
            if filters.get('search'):
                s = filters['search']
                q['$or'] = [{'user':{'$regex':s,'$options':'i'}},{'supervisor':{'$regex':s,'$options':'i'}},
                            {'action':{'$regex':s,'$options':'i'}}]
            total = mdb.audit_logs.count_documents(q)
            rows  = list(mdb.audit_logs.find(q,{'_id':0}).sort('timestamp',-1).skip(offset).limit(limit))
            return rows, total
        else:
            clauses=[]; params=[]
            if filters.get('from_date'): clauses.append('timestamp>=?'); params.append(filters['from_date'])
            if filters.get('to_date'):   clauses.append('timestamp<=?'); params.append(filters['to_date']+' 23:59:59')
            if filters.get('action') and filters['action']!='ALL': clauses.append('action=?'); params.append(filters['action'])
            if filters.get('status') and filters['status']!='ALL': clauses.append('status=?'); params.append(filters['status'])
            if filters.get('shift')  and filters['shift'] !='ALL': clauses.append('shift=?'); params.append(filters['shift'])
            if filters.get('prod_date'): clauses.append('prod_date=?'); params.append(filters['prod_date'])
            if filters.get('search'):
                s='%'+filters['search']+'%'
                clauses.append('(user LIKE ? OR action LIKE ? OR supervisor LIKE ?)'); params+=[s,s,s]
            where=('WHERE '+' AND '.join(clauses)) if clauses else ''
            conn=_sqlite()
            total=conn.execute(f'SELECT COUNT(*) FROM audit_logs {where}',params).fetchone()[0]
            rows=conn.execute(f'SELECT * FROM audit_logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?',
                              params+[limit,offset]).fetchall()
            conn.close()
            result=[]
            for r in rows:
                d=dict(r)
                for f in ('details','data_snapshot','before_data','after_data'):
                    if d.get(f):
                        try: d[f]=json.loads(d[f])
                        except: pass
                result.append(d)
            return result, total
    except Exception as e:
        print(f'[AUDIT] query_logs failed: {e}'); return [], 0

def get_summary(mdb=None, period='today'):
    try:
        now=dt.datetime.now()
        if   period=='today': cutoff=(now - dt.timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        elif period=='week':  cutoff=(now - dt.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        elif period=='month': cutoff=(now - dt.timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        else: cutoff=None

        def cnt(mdb, action, status=None):
            if mdb is not None:
                q={'action':action}
                if cutoff: q['timestamp']={'$gte':cutoff}
                if status: q['status']=status
                return mdb.audit_logs.count_documents(q)
            else:
                w=f"WHERE action='{action}'"
                if cutoff: w+=f" AND timestamp>='{cutoff}'"
                if status: w+=f" AND status='{status}'"
                conn=_sqlite(); r=conn.execute(f'SELECT COUNT(*) FROM audit_logs {w}').fetchone()[0]; conn.close(); return r

        return {
            'ocr_processed': cnt(mdb,'OCR_SUCCESS'),   'ocr_failed':cnt(mdb,'OCR_FAILED'),
            'ocr_fallback':  cnt(mdb,'OCR_FALLBACK'),  'records_saved':cnt(mdb,'RECORD_SAVED'),
            'records_edited':cnt(mdb,'RECORD_EDITED'),  'records_deleted':cnt(mdb,'RECORD_DELETED'),
            'exports':cnt(mdb,'EXCEL_EXPORTED'),         'claude_calls':0, 'gemini_calls':0,
            'total_cost':0, 'avg_confidence':0, 'total_events':0, 'failed_events':0,
        }
    except Exception as e:
        print(f'[AUDIT] summary failed: {e}')
        return {k:0 for k in ['ocr_processed','ocr_failed','ocr_fallback','records_saved',
                               'records_edited','records_deleted','exports','claude_calls',
                               'gemini_calls','total_cost','avg_confidence','total_events','failed_events']}

def distinct_values(mdb=None, field=None):
    allowed={'action','status','shift','prod_date','user','api_used'}
    if field not in allowed: return []
    try:
        if mdb is not None:
            return [v for v in mdb.audit_logs.distinct(field) if v]
        conn=_sqlite()
        rows=conn.execute(f'SELECT DISTINCT {field} FROM audit_logs WHERE {field} IS NOT NULL ORDER BY {field}').fetchall()
        conn.close(); return [r[0] for r in rows if r[0]]
    except Exception as e:
        print(f'[AUDIT] distinct_values failed: {e}'); return []
