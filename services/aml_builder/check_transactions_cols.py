import oracledb

dsn = "192.168.30.3:1521/AIDB"
user = "BI_DWH"
password = "BI_DWH"

try:
    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    cursor = conn.cursor()
    
    # Target scenario to keep
    target_sc = '1783250966668495'
    
    # Get all rule codes of our target scenario
    cursor.execute("""
        SELECT AML_RULE_CODE FROM BI_DWH.PIO_AML_SCENARIO_RULES WHERE AML_SCENARIO = :sc
    """, {"sc": target_sc})
    target_rules = [r[0] for r in cursor.fetchall()]
    print("Target Scenario Rules to KEEP:", target_rules)
    
    # 1. Delete details of other rules
    if target_rules:
        placeholders = ",".join(f"'{r}'" for r in target_rules)
        cursor.execute(f"DELETE FROM BI_DWH.PIO_AML_RULES_DETAILS WHERE RULE_CODE NOT IN ({placeholders})")
        print("Deleted other rules details:", cursor.rowcount)
        
        # 2. Delete other scenario-rules links
        cursor.execute(f"DELETE FROM BI_DWH.PIO_AML_SCENARIO_RULES WHERE AML_SCENARIO != :sc", {"sc": target_sc})
        print("Deleted other scenario-rules links:", cursor.rowcount)
        
        # 3. Delete other rules
        cursor.execute(f"DELETE FROM BI_DWH.PIO_AML_RULES WHERE RULE_CODE NOT IN ({placeholders})")
        print("Deleted other rules:", cursor.rowcount)
        
        # 4. Delete other scenarios
        cursor.execute(f"DELETE FROM BI_DWH.PIO_AML_SCENARIO WHERE SCENARIO_CODE != :sc", {"sc": target_sc})
        print("Deleted other scenarios:", cursor.rowcount)
        
    conn.commit()
    
    # Clean output tables
    cursor.execute("DELETE FROM BI_DWH.PIO_AML_CUSTOMERS")
    cursor.execute("DELETE FROM BI_DWH.PIO_AML_CUSTOMERS_DET")
    conn.commit()
    print("Cleaned alert tables.")

    # Re-run stored procedure
    print("Re-running FILL_PIO_AML_CUSTOMERS...")
    p_status = cursor.var(oracledb.NUMBER)
    cursor.execute("""
        BEGIN
            FILL_PIO_AML_CUSTOMERS(
                COUNTRYCODE => 400,
                INSTCODE => 1,
                P_STATUS => :p_status
            );
        END;
    """, {"p_status": p_status})
    print("Procedure returned status:", p_status.getvalue())
    
    # Query alerts
    cursor.execute("SELECT COUNT(*) FROM BI_DWH.PIO_AML_CUSTOMERS")
    print("Alerts in PIO_AML_CUSTOMERS:", cursor.fetchone()[0])
    
    cursor.execute("SELECT COUNT(*) FROM BI_DWH.PIO_AML_CUSTOMERS_DET")
    print("Alert details in PIO_AML_CUSTOMERS_DET:", cursor.fetchone()[0])

except Exception as e:
    print("Error during execution:", e)
