"""
sap_script.py - SAP GUI Automation for Carding Machine Table
No Excel middleware - reads JSON directly from app.
"""
import sys, time, os, json
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from datetime import datetime

TABLE_ID = "wnd[0]/usr/tblSAPLZROUTE_MACHTCTRL_ZROUTE_MACH"

def wait(ms=300):
    time.sleep(ms / 1000)

def sap_date(date_str):
    s = str(date_str).strip()
    for fmt in ('%d-%m-%Y','%Y-%m-%d','%d/%m/%Y','%d.%m.%Y'):
        try: return datetime.strptime(s, fmt).strftime('%d.%m.%Y')
        except: pass
    return s

def get_session():
    try:
        import win32com.client
        sap  = win32com.client.GetObject("SAPGUI")
        sess = sap.GetScriptingEngine.Children(0).Children(0)
        print(f"[SAP] Connected: {sess.Info.SystemName} | User: {sess.Info.User}")
        return sess
    except Exception as e:
        print(f"[ERROR] Cannot connect to SAP: {e}")
        sys.exit(1)

def go_to_new_entries(session):
    session.findById("wnd[0]").maximize()
    wait(500)
    title = session.findById("wnd[0]/titl").text
    print(f"[SAP] Screen: {title}")
    if "New Entries" in title:
        print("[SAP] Already on New Entries (OK)")
        return
    if "Change View" in title:
        print("[SAP] Change View -> New Entries (F5)...")
        session.findById("wnd[0]/tbar[1]/btn[5]").press()
        wait(1500)
    else:
        print("[SAP] Navigating: zroute -> Edit -> New Entries...")
        session.findById("wnd[0]/tbar[0]/okcd").text = "zroute"
        session.findById("wnd[0]").sendVKey(0)
        wait(2000)
        print(f"[SAP] After zroute: {session.findById('wnd[0]/titl').text}")
        session.findById("wnd[0]/usr/btnUPDATE_PUSH").press()
        wait(1500)
        print(f"[SAP] After Edit: {session.findById('wnd[0]/titl').text}")
        session.findById("wnd[0]/tbar[1]/btn[5]").press()
        wait(1500)
    final = session.findById("wnd[0]/titl").text
    print(f"[SAP] Final: {final}")
    if "New Entries" not in final:
        print("[ERROR] Could not reach New Entries screen automatically.")
        sys.exit(1)
    print("[SAP] New Entries ready (OK)")

def get_visible_rows(session):
    try:
        return session.findById(TABLE_ID).VisibleRowCount
    except:
        return 16

def enter_row(session, row, absolute_idx, screen_row):
    try:
        first = session.findById(f"{TABLE_ID}/ctxtZROUTE_MACH-WERKS[0,{screen_row}]")
        first.setFocus()
        wait(100)
        first.text = row['plnt']
        wait(80)
        session.findById(f"{TABLE_ID}/ctxtZROUTE_MACH-DEPARTMENT[1,{screen_row}]").text = row['dept']
        wait(80)
        session.findById(f"{TABLE_ID}/cmbZROUTE_MACH-MACHINE_CLASS[2,{screen_row}]").key = row['mach_cl']
        wait(80)
        # MATNR is a dropdown (cmb) - confirmed from SAP recording
        session.findById(f"{TABLE_ID}/cmbZROUTE_MACH-MATNR[3,{screen_row}]").key = row['material']
        wait(80)
        try:
            session.findById(f"{TABLE_ID}/ctxtZROUTE_MACH-EFFDATE[4,{screen_row}]").text = sap_date(row['date'])
            wait(150)
        except Exception as e:
            print(f"  [WARN] Date: {e}")
        session.findById(f"{TABLE_ID}/txtZROUTE_MACH-SHIFT[5,{screen_row}]").text = row['shift']
        wait(80)
        for i, wc in enumerate(row['work_ctrs']):
            try:
                session.findById(f"{TABLE_ID}/ctxtZROUTE_MACH-MACHINE_{i+1}[{6+i},{screen_row}]").text = wc
                wait(60)
            except Exception as e:
                print(f"  [WARN] WC{i+1}: {e}"); break
        session.findById("wnd[0]").sendVKey(0)
        wait(300)
        print(f"  [ROW {absolute_idx+1:02d}] OK: {row['plnt']} | {row['mach_cl']} | {row['material']:28} | {row['date']} {row['shift']} | {row['work_ctrs']}")
        return True
    except Exception as e:
        print(f"  [ERROR] Row {absolute_idx+1}: {e}")
        return False

def save(session):
    print("[SAP] Saving...")
    try:
        session.findById("wnd[0]/tbar[0]/btn[11]").press()
        wait(1000)
    except:
        session.findById("wnd[0]").sendVKey(11)
        wait(1000)
    try:
        session.findById("wnd[1]/tbar[0]/btn[0]").press()
        wait(500)
    except:
        pass
    print("[SAP] Saved.")

def main():
    print("=" * 65)
    print("  SAP Carding Machine Auto Entry")
    print("=" * 65)

    rows = None
    json_path = None
    auto = '--auto' in sys.argv

    args = sys.argv[1:]
    if '--json' in args:
        idx = args.index('--json')
        if idx + 1 < len(args):
            json_path = args[idx + 1]

    if json_path and os.path.exists(json_path):
        with open(json_path, 'r') as f:
            rows = json.load(f)
        print(f"[DATA] {len(rows)} rows loaded from JSON")
    else:
        print("[ERROR] No data source found.")
        sys.exit(1)

    if not rows:
        print("[ERROR] No rows to enter.")
        sys.exit(1)

    print(f"\n[PREVIEW] First 5 rows:")
    for r in rows[:5]:
        print(f"  {r['plnt']} | {r['mach_cl']} | {r['material']:28} | {r['date']} {r['shift']} | {r['work_ctrs']}")
    print(f"\n  Total: {len(rows)} rows")

    if not auto:
        if input("\n  Type YES to start > ").strip().upper() != 'YES':
            print("Cancelled."); sys.exit(0)
    else:
        print("[AUTO] Starting...")

    session = get_session()
    go_to_new_entries(session)

    visible = get_visible_rows(session)
    print(f"[SAP] Table visible rows: {visible}")
    print(f"\n[ENTERING] {len(rows)} rows...")

    success, failed, failed_rows = 0, 0, []

    for i, row in enumerate(rows):
        screen_row = i % visible

        if i > 0 and screen_row == 0:
            print(f"  [PAGE] Paging down after row {i}...")
            try:
                session.findById(f"{TABLE_ID}/ctxtZROUTE_MACH-MACHINE_1[6,{visible-1}]").setFocus()
            except:
                pass
            session.findById("wnd[0]").sendVKey(82)
            wait(600)

        ok = enter_row(session, row, i, screen_row)
        if ok:
            success += 1
        else:
            failed += 1
            failed_rows.append(i + 1)
        wait(100)

    if success > 0:
        save(session)

    print("\n" + "=" * 65)
    print(f"  DONE - {success} OK | {failed} failed")
    if failed_rows:
        print(f"  Failed rows: {failed_rows}")
    print("=" * 65)

if __name__ == '__main__':
    main()
