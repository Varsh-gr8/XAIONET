
# receiver/receiver.py
import asyncio, websockets, json, time
import pyttsx3
from functools import partial

WS_URI = "ws://localhost:8765"

# Filter: Minimum number of characters required for a message to be considered real speech.
# Text shorter than this is classified as garbage/silence.
MIN_SPEECH_LENGTH = 5 
SILENCE_TEXT = "No text received"

# Initialize pyttsx3 engine once
try:
    engine = pyttsx3.init()
except Exception as e:
    print(f"Error initializing pyttsx3: {e}")

def adapt_engine(polarity, priority):
    """Synchronous function to set pyttsx3 properties based on data."""
    if priority >= 8:
        engine.setProperty('rate', 200)
    else:
        engine.setProperty('rate', 150)

def synthesize_speech(text, priority, polarity):
    """
    Synchronous function to perform the blocking TTS operation.
    """
    # Use a slightly lower rate for system messages like "No text received"
    if text == SILENCE_TEXT:
        engine.setProperty('rate', 130)
    else:
        adapt_engine(polarity, priority)
        
    print(f"Synthesizing speech: {text[:50]}...")
    engine.say(text)
    engine.runAndWait()

async def run():
    try:
        async with websockets.connect(WS_URI) as ws:
            # 1. Register as a receiver
            await ws.send(json.dumps({"type":"register","role":"receiver","session_id":"receiver1"}))
            print("Receiver registered")
            
            loop = asyncio.get_running_loop()
            
            # 2. Main message loop
            async for msg in ws:
                try:
                    obj = json.loads(msg)
                    if obj.get("type") == "semantic":
                        p = obj["payload"]
                        
                        text = p.get('text', '').strip()
                        priority = p.get("priority", 1) 
                        polarity = p.get("polarity", 0) 

                        # --- SIMPLE FILTER: DROP EMPTY/GARBAGE ---
                        MIN_SPEECH_LENGTH = 5
                        if not text or len(text) < MIN_SPEECH_LENGTH:
                            print(f"[{p.get('session_id', 'N/A')}] FILTERED: silence/garbage skipped.")
                            continue
                        # -----------------------------------------

                        print(f"[{p.get('session_id', 'N/A')}] priority={priority} sentiment={p.get('sentiment')} text={text}")

                        tts_task = loop.run_in_executor(
                            None, 
                            partial(synthesize_speech, text, priority, polarity)
                        )
                        await tts_task 

                        # Latency estimate
                        try:
                            latency = time.time() - float(p.get("capture_ts") or p.get("forward_ts") or time.time())
                            print("approx end-to-end latency (s):", round(latency,3))
                        except:
                            pass
                
                except Exception as e:
                    print("recv err (processing message):", e)

    except ConnectionRefusedError: 
        print(f"Connection refused: Is the server running at {WS_URI}?")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"Connection closed by server: {e}")
    except Exception as e:
        print("An unexpected error occurred:", e)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nReceiver shutting down.")
    finally:
        if 'engine' in locals():
            pass
