"""
identify_columns.py — Find exact SAP column IDs for ZROUTE
===========================================================
Run this FIRST before sap_script.py.
It prints all field IDs in the ZROUTE New Entries grid
so you can confirm the correct column names.

Run: python identify_columns.py
"""

def main():
    try:
        import win32com.client
        sap_gui     = win32com.client.GetObject("SAPGUI")
        application = sap_gui.GetScriptingEngine
        connection  = application.Children(0)
        session     = connection.Children(0)
        print(f"Connected: {session.Info.SystemName}")
    except Exception as e:
        print(f"Cannot connect to SAP GUI: {e}")
        return

    print("\nScanning ZROUTE New Entries screen for all field IDs...")
    print("Make sure ZROUTE New Entries screen is open!\n")

    input("Press Enter when ZROUTE New Entries is open > ")

    try:
        # Get the main window
        wnd = session.FindById("wnd[0]")

        # Try to find the table control
        try:
            usr = session.FindById("wnd[0]/usr")
            print(f"User area children count: {usr.Children.Count}")
            for i in range(usr.Children.Count):
                child = usr.Children(i)
                print(f"  [{i}] ID={child.Id}  Type={child.Type}  Name={getattr(child,'Name','?')}")
        except Exception as e:
            print(f"Error scanning children: {e}")

        # Try standard table IDs
        possible_tables = [
            "wnd[0]/usr/tblSAPLZROUTE_MACHTC_ZROUTE_MACHN",
            "wnd[0]/usr/tblZROUTE_MACH",
            "wnd[0]/usr/tblTC_ZROUTE_MACHN",
        ]
        for tid in possible_tables:
            try:
                t = session.FindById(tid)
                print(f"\nFOUND TABLE: {tid}")
                print(f"  Rows: {t.RowCount}  Cols: {t.ColumnCount}")
                for ci in range(min(t.ColumnCount, 10)):
                    try:
                        col = t.GetCell(0, ci)
                        print(f"  Col[{ci}] ID={col.Id}  Text={col.Text}")
                    except Exception as e:
                        print(f"  Col[{ci}] error: {e}")
                break
            except:
                pass

    except Exception as e:
        print(f"Scan error: {e}")

    print("\nCopy the table ID and column IDs above into sap_script.py")

if __name__ == '__main__':
    main()
