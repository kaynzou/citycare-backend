"""
Handles language detection + translation for incoming complaints.

Two-tier approach:
- langdetect for fast local language detection (no network call)
- deep_translator (Google Translate backend) for the actual translation

deep_translator needs outbound internet access to translate.google.com.
If that's blocked (corporate network, offline demo, etc.) this falls back
to returning the original text untranslated rather than failing the request —
a complaint should never fail to save just because translation didn't work.

For noticeably better quality on Indian regional/dialect text (Bhojpuri,
Awadhi, Magahi, code-mixed Hinglish, etc.), swap translate_text() below to
call an LLM (Claude/GPT) with a translation prompt instead of deep_translator —
see the commented block at the bottom for the pattern.
"""

from langdetect import detect, DetectorFactory, LangDetectException
from deep_translator import GoogleTranslator

DetectorFactory.seed = 0  # deterministic detection


def detect_language(text: str) -> str:
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    """Returns (detected_language, translated_text)."""
    detected = detect_language(text)

    if detected == target_language:
        return detected, text

    try:
        translated = GoogleTranslator(source="auto", target=target_language).translate(text)
        return detected, translated
    except Exception:
        # Network unavailable / API hiccup — never block complaint submission on this.
        return detected, text


# --- LLM-based alternative (better for regional dialects, code-mixed text) ---
#
# import requests
#
# def translate_text_llm(text: str, target_language: str = "English") -> tuple[str, str]:
#     prompt = (
#         f"Detect the language of this text and translate it to {target_language}. "
#         f"Preserve tone and specific details (names, dates, amounts). "
#         f'Return JSON: {{"detected_language": "...", "translation": "..."}}\n\n'
#         f"Text: {text}"
#     )
#     response = requests.post(
#         "https://api.anthropic.com/v1/messages",
#         headers={"x-api-key": "YOUR_KEY", "anthropic-version": "2023-06-01"},
#         json={
#             "model": "claude-sonnet-4-6",
#             "max_tokens": 500,
#             "messages": [{"role": "user", "content": prompt}],
#         },
#     )
#     data = response.json()["content"][0]["text"]
#     parsed = json.loads(data)
#     return parsed["detected_language"], parsed["translation"]
