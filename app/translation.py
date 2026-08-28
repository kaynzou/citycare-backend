import os
from huggingface_hub import InferenceClient

HF_API_KEY = os.environ.get("HUGGINGFACE_API_KEY")
if not HF_API_KEY:
    raise ValueError("Missing HUGGINGFACE_API_KEY environment variable")

client = InferenceClient(token=HF_API_KEY)


def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    if not text or not text.strip():
        return ("unknown", "")

    # Use a dedicated translation model instead of LLM
    # Helsinki-NLP models are lightweight and always available on HF
    model_map = {
        "hi": "Helsinki-NLP/opus-mt-hi-en",
        "ta": "Helsinki-NLP/opus-mt-tam-eng",
        "te": "Helsinki-NLP/opus-mt-te-en",
        "bn": "Helsinki-NLP/opus-mt-bn-en",
        "mr": "Helsinki-NLP/opus-mt-mr-en",
        "gu": "Helsinki-NLP/opus-mt-gu-en",
        "kn": "Helsinki-NLP/opus-mt-kn-en",
        "ml": "Helsinki-NLP/opus-mt-ml-en",
        "pa": "Helsinki-NLP/opus-mt-pa-en",
        "ur": "Helsinki-NLP/opus-mt-ur-en",
    }
    
    # For Hinglish/Tanglish, we'll try Hindi first as fallback
    try:
        # Try direct translation with a multilingual model
        response = client.translation(
            text,
            model="Helsinki-NLP/opus-mt-hi-en",  # Works for Hinglish too
        )
        translated = response[0]["translation_text"]
        return ("hi-romanized", translated)
    except Exception as e:
        print(f"Translation error: {str(e)}")
        return ("unknown", text)
