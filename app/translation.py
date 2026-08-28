import os
import requests

HF_API_KEY = os.environ.get("HUGGINGFACE_API_KEY")
if not HF_API_KEY:
    raise ValueError("Missing HUGGINGFACE_API_KEY environment variable")

def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    """
    Simple translation using Hugging Face Inference API.
    Handles Hinglish/Tanglish by using a prompt-based approach.
    """
    if not text or not text.strip():
        return ("unknown", "")
    
    # Use a simple, reliable LLM that's always available
    API_URL = "https://api-inference.huggingface.co/models/google/flan-t5-base"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    
    # Simple prompt for translation
    prompt = f"Translate to {target_language}: {text}"
    
    try:
        print(f"🔄 Translating: '{text}'")
        
        response = requests.post(
            API_URL,
            headers=headers,
            json={"inputs": prompt},
            timeout=30
        )
        
        if response.status_code != 200:
            print(f" HF API Error {response.status_code}: {response.text}")
            return ("unknown", text)
        
        result = response.json()
        translated = result[0]["generated_text"].strip()
        
        print(f"✅ Translated to: '{translated}'")
        return ("romanized", translated)
        
    except Exception as e:
        print(f"❌ Translation failed: {str(e)}")
        return ("unknown", text)
