from flask import Flask, request, render_template, jsonify
from flask_socketio import SocketIO
import requests

# Correction 1: Use __name__
app = Flask(__name__)
# Ensure the secret key is set for production, but kept simple here for development
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")
NODE_OVERRIDE_URL = "http://localhost:8000/override"

@app.route("/")
def index():
    # NOTE: This requires a 'templates/index.html' file to exist.
    return render_template("index.html")

@app.route("/update", methods=["POST"])
def update():
    """Endpoint called by the data source to push new data to the dashboard."""
    data = request.json
    # Emit data to all connected clients via Socket.IO
    socketio.emit("update", data)
    return "OK"

@app.route("/override", methods=["POST"])
def override():
    """Endpoint for the dashboard to send manual override commands to the Node API."""
    j = request.json
    try:
        # Forward the request to the upstream Node API
        r = requests.post(NODE_OVERRIDE_URL, json=j, timeout=1.0)
        
        # Check if the response is JSON before calling r.json()
        if r.status_code == 200 and 'application/json' in r.headers.get('Content-Type', ''):
             return jsonify(r.json()), r.status_code
        else:
             # Handle non-JSON errors from the Node API
             return jsonify({"ok": False, "error": f"Node API returned non-JSON response or status {r.status_code}", "detail": r.text}), 500

    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": "Could not connect to Node API (port 8000). Is it running?"}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    print("Starting dashboard on http://0.0.0.0:5000")
    # Use socketio.run for integrated WebSocket and Flask serving
    socketio.run(app, host="0.0.0.0", port=5000)

