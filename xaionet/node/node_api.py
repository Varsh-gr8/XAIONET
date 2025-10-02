# node/node_api.py
from flask import Flask, request, jsonify
import sqlite3, os, time

app = Flask(__name__)

# Assumes node_api.py is in 'node/' and 'xaionet' is the parent directory (..).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "db", "xaionet.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Function to ensure the database table exists on initialization
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS overrides(
        session_id TEXT PRIMARY KEY, 
        priority INTEGER, 
        ts REAL
    )""")
    conn.commit()
    conn.close()

# Run database initialization once on script start
init_db()

def get_conn():
    # Gets a connection to the database
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    return c
    
# FIX 1: Add a simple root route to prevent 404s when a browser hits the base URL
@app.route("/", methods=["GET"], endpoint="root_check")
def root():
    return jsonify({"service": "XAIONET Node API", "status": "running", "endpoints": ["/status (GET)", "/override (POST)"]}), 200

# Added explicit endpoint name for robustness
@app.route("/override", methods=["POST"], endpoint="override_priority")
def override():
    try:
        j = request.json or {}
        session_id = j.get("session_id")
        priority = int(j.get("priority", 1))
        
        if not session_id:
            return jsonify({"ok": False, "error": "missing session_id"}), 400
            
        conn = get_conn()
        cur = conn.cursor()
        # INSERT OR REPLACE handles existing session_ids gracefully
        cur.execute("INSERT OR REPLACE INTO overrides(session_id,priority,ts) VALUES (?,?,?)", (session_id, priority, time.time()))
        conn.commit()
        conn.close()
        
        return jsonify({"ok": True, "session_id": session_id, "priority": priority})
        
    except Exception as e:
        # Catch unexpected errors like JSON parsing failure
        print(f"Error processing override request: {e}")
        return jsonify({"ok": False, "error": f"Internal API Error (check request body format): {str(e)}"}), 500

# Added explicit endpoint name for robustness
@app.route("/status", methods=["GET"], endpoint="get_status")
def status():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT session_id,priority,ts FROM overrides")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"overrides": [{"session_id": r[0], "priority": r[1], "ts": r[2]} for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Database Error: {str(e)}"}), 500

if __name__ == "__main__":
    print("Starting node API on http://0.0.0.0:8000")
    # CRITICAL: debug=True is active to help diagnose future errors
    app.run(port=8000, host="0.0.0.0", debug=True)
