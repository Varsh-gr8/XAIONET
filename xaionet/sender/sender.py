import argparse, asyncio, json, time, io
import sounddevice as sd
import soundfile as sf
import websockets

# Configuration
SR = 16000
CHUNK_SECS = 5.0

async def send_loop(ws_uri, session_id):
    try:
        async with websockets.connect(ws_uri) as ws:
            # 1. Register
            await ws.send(json.dumps({"type":"register","role":"sender","session_id":session_id}))
            print("registered as sender:", session_id)
            
            # 2. Main send loop
            while True:
                # User feedback: Confirm recording has started
                print(f"[{time.strftime('%H:%M:%S')}] Recording {CHUNK_SECS}s chunk...")
                
                # Record the audio chunk
                data = sd.rec(int(CHUNK_SECS * SR), samplerate=SR, channels=1, dtype='int16')
                sd.wait() # Block and wait for recording to finish

                # Convert audio to WAV bytes
                buf = io.BytesIO()
                sf.write(buf, data, SR, format='WAV')
                wav_bytes = buf.getvalue()
                
                # Create and send header
                header = {
                    "type":"audio_chunk",
                    "session_id":session_id,
                    "capture_ts": time.time(), 
                    "audio_size": len(wav_bytes)
                }
                await ws.send(json.dumps(header))
                
                # Send binary audio data
                await ws.send(wav_bytes) 
                
                await asyncio.sleep(0.01) # Small pause
                
    except ConnectionRefusedError:
        print(f"Error: Connection refused. Is the server running at {ws_uri}?")
    except Exception as e:
        print(f"An error occurred in sender loop: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIONETx WebSocket Sender Client.")
    parser.add_argument("--ws", default="ws://localhost:8765", help="WebSocket URI for the AIONETx server.")
    parser.add_argument("--session", default="call1", help="Session ID for this sender.")
    args = parser.parse_args()
    
    try:
        asyncio.run(send_loop(args.ws, args.session))
    except KeyboardInterrupt:
        print("\nSender shutting down.")
