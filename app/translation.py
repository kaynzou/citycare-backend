"""
Handles language detection + translation for incoming complaints.

Uses Hugging Face Inference API (gemma-2b-it) for robust translation.
This approach handles code-mixed text (Hinglish, Tanglish) and regional 
dialects far better than scraping-based translators, and won't get 
blocked by cloud IP filters on Render.
"""

import os
from huggingface_hub import InferenceClient

# Initialize client once at module level to reuse connections
HF_API_KEY = os.environ.get("HUGGINGFACE_API_KEY")
if not HF_API_KEY:
    raise ValueError("Missing HUGGINGFACE_API_KEY environment variable")

client = InferenceClient(token=HF_API_KEY)


def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    """
    Returns (detected_language, translated_text).
    Uses an LLM prompt to handle Romanized regional languages (Hinglish/Tanglish).
    Falls back to original text if translation fails so complaints never fail to save.
    """
    if not text or not text.strip():
        return ("unknown", "")

    # Smart prompt that explicitly handles English-script regional languages
    prompt = (
        f"You are an expert translator for Indian regional languages. "
        f"Translate the following complaint to {target_language}.\n\n"
        f"CRITICAL RULES:\n"
        f"1. The text may be a regional language (Hindi, Tamil, Telugu, etc.) typed using English letters (Hinglish/Tanglish).\n"
        f"2. If typed in English letters, translate the INTENDED meaning, not literal English words.\n"
        f"3. If already in {target_language}, return it exactly as-is.\n"
        f"4. Output ONLY the translated text. No explanations.\n\n"
        f'Text: "{text}"\n\n'
        f"Translated:"
    )

    try:
        response = client.text_generation(
            prompt,
            model="google/gemma-2b-it",  # Free-tier friendly, great at mixed scripts
            max_new_tokens=250,
            temperature=0.1,  # Low temp for consistent translations
        )
        
        translated = response.strip()
        # Gemma doesn't do explicit lang detection, so we mark as romanized/mixed
        return ("romanized/regional", translated)

    except Exception as e:
        print(f"⚠️ Translation failed: {str(e)}")
        # Never block complaint submission on translation failure
        return ("unknown", text)
