"""
Handles language detection + translation for incoming complaints.

Two-tier approach:
- langdetect for local language detection (no network call)
- deep_translator (Google Translate backend) for the actual translation

REAL BUG FOUND IN PRODUCTION (confirmed via live Render logs): Google
periodically rate-limits or blocks requests from cloud/datacenter IPs
(exactly what Render is). When that happens, deep_translator's underlying
HTTP call gets back Google's HTML error page instead of a translation —
and instead of raising an exception, it was parsing that error page's
text and returning it as if it were a real translation.

THE FIX: after getting a result back, check it against known Google error
page signatures. If it matches, treat this exactly like a network failure —
return the original, untranslated text.
"""

from langdetect import detect_langs, DetectorFactory, LangDetectException
from deep_translator import GoogleTranslator

DetectorFactory.seed = 0

EXPECTED_LANGUAGES = {
    "en", "hi", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "ur", "or", "as", "ne",
}

MIN_CONFIDENCE = 0.85
MIN_CHARS_FOR_LANGDETECT = 15

GOOGLE_ERROR_SIGNATURES = [
    "error 500",
    "server error",
    "that's an error",
    "that's all we know",
    "please try again later",
]


def detect_language(text: str) -> str:
    if len(text.strip()) < MIN_CHARS_FOR_LANGDETECT:
        return "unknown"
    try:
        guesses = detect_langs(text)
        if not guesses:
            return "unknown"
        top = guesses[0]
        if top.lang in EXPECTED_LANGUAGES and top.prob >= MIN_CONFIDENCE:
            return top.lang
        return "unknown"
    except LangDetectException:
        return "unknown"


def _looks_like_google_error_page(text: str) -> bool:
    lowered = text.lower()
    return any(signature in lowered for signature in GOOGLE_ERROR_SIGNATURES)


def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    detected = detect_language(text)

    if detected == target_language:
        return detected, text

    try:
        translated = GoogleTranslator(source="auto", target=target_language).translate(text)

        if not translated or _looks_like_google_error_page(translated):
            return detected, text

        return detected, translated
    except Exception:
        return detected, text
