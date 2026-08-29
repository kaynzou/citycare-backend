"""
Handles language detection + translation for incoming complaints.

Two-tier approach:
- langdetect for local language detection (no network call)
- deep_translator (Google Translate backend) for the actual translation

TRANSLATION ITSELF WORKS FINE — confirmed on the live deployment translating
romanized Hindi ("Mera pani ka connection kat gaya") to correct English.
Google Translate's own internal detection is good; it doesn't rely on the
separate `detected_language` label below.

THE ONLY BUG: the separately-reported `detected_language` field (using
langdetect) can be confidently wrong on short/romanized text — it has
labeled real complaints as Swedish, Somali, and Tagalog. This does NOT
affect translation quality, only the label shown alongside it.

THE FIX: only trust langdetect's guess when it's both (a) a language this
app actually expects to see, and (b) langdetect itself is confident. If
either fails, report "unknown" instead of a wrong confident label — better
to admit uncertainty than show "tl" for Hindi.

deep_translator needs outbound internet access to translate.google.com.
If that's blocked, this falls back to returning the original text
untranslated rather than failing the request — a complaint should never
fail to save just because translation didn't work.
"""

from langdetect import detect_langs, DetectorFactory, LangDetectException
from deep_translator import GoogleTranslator

DetectorFactory.seed = 0  # deterministic detection

EXPECTED_LANGUAGES = {
    "en", "hi", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "ur", "or", "as", "ne",
}

MIN_CONFIDENCE = 0.85
MIN_CHARS_FOR_LANGDETECT = 15


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


def translate_text(text: str, target_language: str = "en") -> tuple[str, str]:
    """Returns (detected_language, translated_text)."""
    detected = detect_language(text)

    if detected == target_language:
        return detected, text

    try:
        translated = GoogleTranslator(source="auto", target=target_language).translate(text)
        return detected, translated
    except Exception:
        return detected, text
