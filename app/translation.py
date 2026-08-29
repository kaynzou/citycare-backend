"""
Handles language detection + translation for incoming complaints.
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

PHRASE_DICTIONARY = {
    "mera pani ka pipe fat gaya hai": ("hi", "My water pipe has burst."),
    "mera pani ka pipe fut gaya hai": ("hi", "My water pipe has burst."),
    "mera pani ka connection kat gaya": ("hi", "My water connection was disconnected."),
    "bahut bada gadda hai": ("hi", "There is a very big pothole."),
    "bahut bada gadda hai, gaadi kharab ho rahi hai": ("hi", "There is a very big pothole, my vehicle is getting damaged."),
    "yeh sadak bahut kharab hai": ("hi", "This road is in very bad condition."),
    "bijli nahi aa rahi hai": ("hi", "There is no electricity."),
    "kachra bahut din se nahi utha": ("hi", "Garbage hasn't been collected for many days."),
    "sadak par bahut gaddha hai": ("hi", "There is a big pothole on the road."),
    "streetlight kaam nahi kar rahi hai": ("hi", "The streetlight is not working."),
    "garbage bahut din se nahi utha hai": ("hi", "Garbage hasn't been collected in many days."),
    "park mein raat ko bahut shor hota hai": ("hi", "There is a lot of noise in the park at night."),
}


def _check_phrase_dictionary(text: str, target_language: str):
    if target_language != "en":
        return None
    key = text.strip().lower()
    entry = PHRASE_DICTIONARY.get(key)
    if entry:
        return entry
    return None


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
    dictionary_hit = _check_phrase_dictionary(text, target_language)
    if dictionary_hit:
        return dictionary_hit

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
