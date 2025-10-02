'''

import asyncio, websockets, json, tempfile, time, os, sqlite3, requests
from concurrent.futures import ProcessPoolExecutor
from textblob import TextBlob
import whisper
import torch # <--- ADDED: Import torch to check for CUDA

# ================== Configuration ==================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "db", "xaionet.db")
DASHBOARD_UPDATE_URL = "http://localhost:5000/update"
MODEL_NAME = "small"
PRIORITY_KEYWORDS = {"help","emergency","urgent","accident","fire","hospital"}

# Executor setup
# CRITICAL FIX: Set to 2 workers for 4GB RTX 3050 to prevent CUDA Out of Memory
executor = ProcessPoolExecutor(max_workers=2)

# ================== Setup Database ==================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    capture_ts REAL,
    transcribe_ts REAL,
    forward_ts REAL,
    audio_bytes INTEGER,
    text_bytes INTEGER,
    text TEXT,
    sentiment REAL,
    priority INTEGER
)""")
c.execute("""CREATE TABLE IF NOT EXISTS overrides(
    session_id TEXT PRIMARY KEY,
    priority INTEGER,
    ts REAL
)""")
conn.commit()
conn.close()

# ================== Globals ==================
senders = set()
receivers = set()
pending_headers = {}

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# Module-level function for safe posting to dashboard (Fixes "Can't pickle" error)
def post_to_dashboard_safe(payload):
    """Sends transcription payload to the dashboard API."""
    import requests
    try:
        requests.post(DASHBOARD_UPDATE_URL, json=payload, timeout=0.5)
    except Exception:
        pass

# Module-level function for process-safe transcription (Fixes Whisper crash)
def blocking_transcribe_audio(audio_file, model_name):
    """Loads the model locally in a new process and performs transcription."""
    import whisper
    # Import torch locally inside the function for ProcessPoolExecutor safety
    import torch 
    
    # CRITICAL GPU CHANGE: Check for GPU and set device
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading Whisper model on device: {device}")
    
    try:
        # Load model onto the selected device (GPU or CPU)
        local_model = whisper.load_model(model_name, device=device)
        
        # --- CRITICAL FIXES FOR REPETITION AND ACCURACY ---
        initial_prompt = "The speaker is having a professional conversation about the network and AI."
        
        # FIX: Removed 'suppress_model_warnings' which caused the error
        options = {
            "without_timestamps": True,
            "initial_prompt": initial_prompt
        }
        
        return local_model.transcribe(audio_file, **options)
        # --------------------------------------------------

    except Exception as e:
        print(f"Whisper transcription failed in worker process: {e}")
        return {"text": ""}

# ================== Processing ==================
async def process_chunk(header, audio_bytes):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(audio_bytes)
    tmp.flush()
    tmp.close()

    loop = asyncio.get_running_loop()
    text = ""
    try:
        # Use the process-safe transcription function
        result = await loop.run_in_executor(executor, blocking_transcribe_audio, tmp.name, MODEL_NAME)
        text = result.get("text", "").strip()
    except Exception as e:
        print("Transcription error:", e)
        text = ""
    trans_end = time.time()

    # --- SILENCE CHECK (The server will now log this and continue) ---
    if not text:
        print("INFO: No speech detected in chunk. Skipping full processing.")
        # Clean up temp file
        try:
            os.remove(tmp.name)
        except Exception:
            pass
        return # Exit the function early if no text was transcribed
    # ------------------------------------------------------------------

    polarity = TextBlob(text).sentiment.polarity
    sentiment = "positive" if polarity > 0.1 else "negative" if polarity < -0.1 else "neutral"
    session_id = header.get("session_id")

    conn_r = get_conn()
    c_r = conn_r.cursor()
    c_r.execute("SELECT priority FROM overrides WHERE session_id=?", (session_id,))
    r = c_r.fetchone()
    conn_r.close()

    if r:
        priority = int(r[0])
    else:
        low = text.lower()
        priority = 10 if any(k in low for k in PRIORITY_KEYWORDS) else (7 if polarity < -0.6 else 1)

    payload = {
        "session_id": session_id,
        "text": text,
        "sentiment": sentiment,
        "polarity": polarity,
        "priority": priority,
        "capture_ts": header.get("capture_ts"),
        "transcribe_ts": trans_end,
        "forward_ts": time.time(),
        "audio_bytes": len(audio_bytes),
        "text_bytes": len(text.encode("utf-8"))
    }

    # Insert to DB
    conn_w = get_conn()
    c_w = conn_w.cursor()
    try:
        c_w.execute("""INSERT INTO logs(session_id,capture_ts,transcribe_ts,forward_ts,audio_bytes,text_bytes,text,sentiment,priority)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                   (session_id, header.get("capture_ts"), trans_end, payload["forward_ts"], payload["audio_bytes"], payload["text_bytes"], text, polarity, priority))
        conn_w.commit()
    except Exception as e:
        print("DB insertion error:", e)
    finally:
        conn_w.close()

    # Forward to Dashboard (Use the module-level function)
    await loop.run_in_executor(executor, post_to_dashboard_safe, payload)

    # Broadcast to receivers
    msg = json.dumps({"type": "semantic", "payload": payload})
    websockets_to_remove = []
    send_tasks = []
    for r in list(receivers):
        async def safe_send(ws, message):
            try:
                await ws.send(message)
            except Exception:
                websockets_to_remove.append(ws)
        send_tasks.append(safe_send(r, msg))
    await asyncio.gather(*send_tasks)
    for r in websockets_to_remove:
        receivers.discard(r)

    # Clean up temp file
    try:
        os.remove(tmp.name)
    except Exception:
        pass

# ================== WebSocket Handler ==================
async def handler(ws):
    session_id = None
    role = "unknown"
    try:
        reg = await ws.recv()
        regobj = json.loads(reg)
        if regobj.get("type") != "register":
            await ws.close()
            return

        role = regobj.get("role")
        session_id = regobj.get("session_id")

        if role == "sender":
            senders.add(ws)
            print("Sender connected:", session_id)
        elif role == "receiver":
            receivers.add(ws)
            print("Receiver connected")
        else:
            await ws.close()
            return

        async for message in ws:
            if isinstance(message, str):
                try:
                    obj = json.loads(message)
                    if obj.get("type") == "audio_chunk":
                        if "session_id" not in obj:
                            obj["session_id"] = session_id
                        pending_headers[ws] = obj
                except Exception:
                    continue
            else:
                header = pending_headers.pop(ws, {"session_id": session_id, "capture_ts": time.time()})
                asyncio.create_task(process_chunk(header, message))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"Handler error for session {session_id} ({role}): {e}")
    finally:
        senders.discard(ws)
        receivers.discard(ws)
        pending_headers.pop(ws, None)

# ================== Main ==================
async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765, max_size=None):
        print("WebSocket server running on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shutting down.")
    finally:
        executor.shutdown(wait=True)
'''
import asyncio, websockets, json, tempfile, time, os, sqlite3, requests
from concurrent.futures import ProcessPoolExecutor
from textblob import TextBlob
import whisper
import torch 

# ================== Configuration ==================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "db", "xaionet.db")
DASHBOARD_UPDATE_URL = "http://localhost:5000/update"
MODEL_NAME = "small"
PRIORITY_KEYWORDS = {"help","emergency","urgent","accident","fire","hospital"}

# Executor setup
executor = ProcessPoolExecutor(max_workers=2)

# ================== Setup Database ==================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    capture_ts REAL,
    transcribe_ts REAL,
    forward_ts REAL,
    audio_bytes INTEGER,
    text_bytes INTEGER,
    text TEXT,
    sentiment REAL,
    priority INTEGER
)""")
c.execute("""CREATE TABLE IF NOT EXISTS overrides(
    session_id TEXT PRIMARY KEY,
    priority INTEGER,
    ts REAL
)""")
conn.commit()
conn.close()

# ================== Globals ==================
senders = set()
receivers = set()
pending_headers = {}

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# Module-level function for safe posting to dashboard (Fixes "Can't pickle" error)
def post_to_dashboard_safe(payload):
    """Sends transcription payload to the dashboard API."""
    import requests
    try:
        requests.post(DASHBOARD_UPDATE_URL, json=payload, timeout=0.5)
    except Exception:
        pass

# Module-level function for process-safe transcription (Fixes Whisper crash)
def blocking_transcribe_audio(audio_file, model_name):
    """Loads the model locally in a new process and performs transcription."""
    import whisper
    import torch 
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading Whisper model on device: {device}")
    
    try:
        local_model = whisper.load_model(model_name, device=device)
        
        # --- FINAL FIXES FOR ACCURACY, REPETITION, AND SILENCE OUTPUT ---
        initial_prompt = "No Transcribed text"
        
        options = {
            "without_timestamps": True,
            "initial_prompt": initial_prompt,
            # Keeping logprob_threshold and temperature for quality, but removing complex result handling
            "logprob_threshold": -1.0, 
            "temperature": 0.0, 
            "suppress_tokens": "-1" 
        }
        
        result = local_model.transcribe(audio_file, **options)
        
        # SIMPLIFIED RETURN: Only return text. Confidence filtering moves to the receiver.
        return {"text": result.get("text", "").strip()}

    except Exception as e:
        print(f"Whisper transcription failed in worker process: {e}")
        return {"text": ""}

# ================== Processing ==================
async def process_chunk(header, audio_bytes):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(audio_bytes)
    tmp.flush()
    tmp.close()

    loop = asyncio.get_running_loop()
    text = ""
    try:
        # Use the process-safe transcription function
        result = await loop.run_in_executor(executor, blocking_transcribe_audio, tmp.name, MODEL_NAME)
        text = result.get("text", "").strip()
    except Exception as e:
        print("Transcription error:", e)
        text = ""
    trans_end = time.time()

    # --- SERVER-SIDE SILENCE & GARBAGE FILTER ---
    MIN_SPEECH_LENGTH = 5
    if not text or len(text) < MIN_SPEECH_LENGTH:
        print("INFO: No meaningful speech detected. Dropping this chunk.")
        try:
            os.remove(tmp.name)
        except Exception:
            pass
        return
    # --------------------------------------------

    polarity = TextBlob(text).sentiment.polarity
    sentiment = "positive" if polarity > 0.1 else "negative" if polarity < -0.1 else "neutral"
    session_id = header.get("session_id")

    conn_r = get_conn()
    c_r = conn_r.cursor()
    c_r.execute("SELECT priority FROM overrides WHERE session_id=?", (session_id,))
    r = c_r.fetchone()
    conn_r.close()

    if r:
        priority = int(r[0])
    else:
        low = text.lower()
        priority = 10 if any(k in low for k in PRIORITY_KEYWORDS) else (7 if polarity < -0.6 else 1)

    payload = {
        "session_id": session_id,
        "text": text,
        "sentiment": sentiment,
        "polarity": polarity,
        "priority": priority,
        "capture_ts": header.get("capture_ts"),
        "transcribe_ts": trans_end,
        "forward_ts": time.time(),
        "audio_bytes": len(audio_bytes),
        "text_bytes": len(text.encode("utf-8"))
    }

    # Insert to DB
    conn_w = get_conn()
    c_w = conn_w.cursor()
    try:
        c_w.execute("""INSERT INTO logs(session_id,capture_ts,transcribe_ts,forward_ts,audio_bytes,text_bytes,text,sentiment,priority)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                   (session_id, header.get("capture_ts"), trans_end, payload["forward_ts"],
                    payload["audio_bytes"], payload["text_bytes"], text, polarity, priority))
        conn_w.commit()
    except Exception as e:
        print("DB insertion error:", e)
    finally:
        conn_w.close()

    # Forward to Dashboard
    await loop.run_in_executor(executor, post_to_dashboard_safe, payload)

    # Broadcast to receivers
    msg = json.dumps({"type": "semantic", "payload": payload})
    websockets_to_remove = []
    send_tasks = []
    for r in list(receivers):
        async def safe_send(ws, message):
            try:
                await ws.send(message)
            except Exception:
                websockets_to_remove.append(ws)
        send_tasks.append(safe_send(r, msg))
    await asyncio.gather(*send_tasks)
    for r in websockets_to_remove:
        receivers.discard(r)

    # Clean up temp file
    try:
        os.remove(tmp.name)
    except Exception:
        pass

# ================== WebSocket Handler ==================
async def handler(ws):
    session_id = None
    role = "unknown"
    try:
        reg = await ws.recv()
        regobj = json.loads(reg)
        if regobj.get("type") != "register":
            await ws.close()
            return

        role = regobj.get("role")
        session_id = regobj.get("session_id")

        if role == "sender":
            senders.add(ws)
            print("Sender connected:", session_id)
        elif role == "receiver":
            receivers.add(ws)
            print("Receiver connected")
        else:
            await ws.close()
            return

        async for message in ws:
            if isinstance(message, str):
                try:
                    obj = json.loads(message)
                    if obj.get("type") == "audio_chunk":
                        if "session_id" not in obj:
                            obj["session_id"] = session_id
                        pending_headers[ws] = obj
                except Exception:
                    continue
            else:
                header = pending_headers.pop(ws, {"session_id": session_id, "capture_ts": time.time()})
                asyncio.create_task(process_chunk(header, message))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"Handler error for session {session_id} ({role}): {e}")
    finally:
        senders.discard(ws)
        receivers.discard(ws)
        pending_headers.pop(ws, None)

# ================== Main ==================
async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765, max_size=None):
        print("WebSocket server running on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shutting down.")
    finally:
        executor.shutdown(wait=True)
