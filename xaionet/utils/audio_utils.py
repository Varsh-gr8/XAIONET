# utils/audio_utils.py
import io
import soundfile as sf

def wav_bytes_from_array(np_audio, samplerate=16000):
    """
    Convert numpy array to WAV bytes (mono).
    """
    buf = io.BytesIO()
    sf.write(buf, np_audio, samplerate, format='WAV')
    return buf.getvalue()

