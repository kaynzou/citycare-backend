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

# Demo-reliability fast path. Dictionary lookups bypass language detection
# entirely (the language code is hardcoded per entry), so these work
# regardless of what langdetect or Google would guess.
PHRASE_DICTIONARY = {
    # --- Water ---
    "mera pani ka pipe fat gaya hai": ("hi", "My water pipe has burst."),
    "mera pani ka pipe fut gaya hai": ("hi", "My water pipe has burst."),
    "mera pani ka connection kat gaya": ("hi", "My water connection was disconnected."),

    # --- Roads / Potholes ---
    "bahut bada gadda hai": ("hi", "There is a very big pothole."),
    "bahut bada gadda hai, gaadi kharab ho rahi hai": ("hi", "There is a very big pothole, my vehicle is getting damaged."),
    "yeh sadak bahut kharab hai": ("hi", "This road is in very bad condition."),
    "sadak par bahut gaddha hai": ("hi", "There is a big pothole on the road."),

    # --- Electricity ---
    "bijli nahi aa rahi hai": ("hi", "There is no electricity."),

    # --- Garbage / Sanitation ---
    "kachra bahut din se nahi utha": ("hi", "Garbage hasn't been collected for many days."),
    "garbage bahut din se nahi utha hai": ("hi", "Garbage hasn't been collected in many days."),
    "hamare ilake mein safai nahi hoti hai": ("hi", "There is no cleanliness/sanitation maintained in our area."),
    "sarvajanik shauchalay bahut ganda hai": ("hi", "The public toilet is very dirty."),

    # --- Street Lights ---
    "streetlight kaam nahi kar rahi hai": ("hi", "The streetlight is not working."),

    # --- Community Problem ---
    "park mein raat ko bahut shor hota hai": ("hi", "There is a lot of noise in the park at night."),

    # --- Crime / Public Safety ---
    "is ilake mein raat ko chori hoti hai": ("hi", "There are thefts in this area at night."),
    "hamari sadak par khula manhole hai jo khatarnak hai": ("hi", "There is an open manhole on our street which is dangerous."),

    # --- Infrastructure ---
    "bridge ka kaam bahut dinon se ruka hai": ("hi", "The bridge construction work has been stalled for many days."),

    # --- Traffic ---
    "hamare chowraha par traffic signal kaam nahi kar raha hai": ("hi", "The traffic signal at our intersection is not working."),

    # --- Other (generic catch-all) ---
    "yeh samasya bahut dinon se hal nahi hui hai": ("hi", "This problem has not been resolved for many days."),

    # ================= Native-script phrases: major Indian languages =================
    # One phrase per category, per language. Bengali/Tamil/Telugu/Marathi/Gujarati/
    # Kannada/Malayalam/Punjabi/Urdu verified against langdetect at high confidence
    # during testing. Odia and Assamese are added directly since langdetect's model
    # can't reliably auto-detect either (Odia isn't supported at all; Assamese shares
    # a script with Bengali and gets misread as Bengali) — dictionary lookup bypasses
    # that limitation entirely since the language code is hardcoded here.

    # Roads/Pothole
    "எனது வீட்டிற்கு அருகில் குப்பை அகற்றப்படவில்லை": ("ta", "Garbage has not been removed near my house."),
    "మా వీధిలో పెద్ద గొయ్యి ఉంది": ("te", "There is a big pothole in our street."),
    "आमच्या रस्त्यावर मोठा खड्डा आहे": ("mr", "There is a big pothole on our road."),
    "ನಮ್ಮ ರಸ್ತೆಯಲ್ಲಿ ದೊಡ್ಡ ಗುಂಡಿ ಇದೆ": ("kn", "There is a big pothole on our road."),
    "എന്റെ വീടിനടുത്ത് വലിയ കുഴി ഉണ്ട്": ("ml", "There is a big pothole near my house."),
    "ਸਾਡੀ ਗਲੀ ਵਿੱਚ ਵੱਡਾ ਟੋਆ ਹੈ": ("pa", "There is a big pothole in our street."),
    "ہماری گلی میں بڑا گڑھا ہے": ("ur", "There is a big pothole in our street."),
    "ଆମ ରାସ୍ତାରେ ବଡ଼ ଗାତ ଅଛି": ("or", "There is a big pothole on our road."),
    "আমাৰ পথত ডাঙৰ গাঁত আছে": ("as", "There is a big pothole on our road."),

    # Water / Electricity
    "আমার এলাকায় বিদ্যুৎ নেই": ("bn", "There is no electricity in my area."),
    "અમારા વિસ્તારમાં પાણી નથી": ("gu", "There is no water in our area."),

    # Sanitation
    "আমাদের এলাকায় জনসাধারণের শৌচালয়টি অত্যন্ত নোংরা": ("bn", "The public toilet in our area is very dirty."),
    "எங்கள் பகுதியில் பொது கழிப்பறை மிகவும் அசுத்தமாக உள்ளது": ("ta", "The public toilet in our area is very dirty."),
    "మా ప్రాంతంలో ప్రజా మరుగుదొడ్డి చాలా మురికిగా ఉంది": ("te", "The public toilet in our area is very dirty."),
    "आमच्या भागातील सार्वजनिक शौचालय खूप घाणेरडे आहे": ("mr", "The public toilet in our area is very dirty."),
    "અમારા વિસ્તારમાં જાહેર શૌચાલય ખૂબ ગંદુ છે": ("gu", "The public toilet in our area is very dirty."),
    "ನಮ್ಮ ಪ್ರದೇಶದಲ್ಲಿ ಸಾರ್ವಜನಿಕ ಶೌಚಾಲಯ ತುಂಬಾ ಕೊಳಕಾಗಿದೆ": ("kn", "The public toilet in our area is very dirty."),
    "ഞങ്ങളുടെ പ്രദേശത്തെ പൊതു ശൗചാലയം വളരെ വൃത്തികെട്ടതാണ്": ("ml", "The public toilet in our area is very dirty."),
    "ਸਾਡੇ ਇਲਾਕੇ ਵਿੱਚ ਜਨਤਕ ਪਖਾਨਾ ਬਹੁਤ ਗੰਦਾ ਹੈ": ("pa", "The public toilet in our area is very dirty."),
    "ہمارے علاقے میں عوامی بیت الخلاء بہت گندا ہے": ("ur", "The public toilet in our area is very dirty."),
    "ଆମ ଅଞ୍ଚଳରେ ସର୍ବସାଧାରଣ ଶୌଚାଳୟ ବହୁତ ଅପରିଷ୍କାର": ("or", "The public toilet in our area is very dirty."),
    "আমাৰ অঞ্চলৰ ৰাজহুৱা শ্বৌচাগাৰটো বৰ লেতেৰা": ("as", "The public toilet in our area is very dirty."),

    # Public Safety
    "আমাদের রাস্তায় একটি খোলা ম্যানহোল আছে যা বিপজ্জনক": ("bn", "There is an open manhole on our street which is dangerous."),
    "எங்கள் தெருவில் ஆபத்தான திறந்த மேன்ஹோல் உள்ளது": ("ta", "There is a dangerous open manhole on our street."),
    "మా వీధిలో ప్రమాదకరమైన తెరిచిన మ్యాన్‌హోల్ ఉంది": ("te", "There is a dangerous open manhole on our street."),
    "आमच्या रस्त्यावर धोकादायक उघडे मॅनहोल आहे": ("mr", "There is a dangerous open manhole on our street."),
    "અમારી શેરીમાં ખતરનાક ખુલ્લું મેનહોલ છે": ("gu", "There is a dangerous open manhole on our street."),
    "ನಮ್ಮ ಬೀದಿಯಲ್ಲಿ ಅಪಾಯಕಾರಿ ತೆರೆದ ಮ್ಯಾನ್‌ಹೋಲ್ ಇದೆ": ("kn", "There is a dangerous open manhole on our street."),
    "ഞങ്ങളുടെ തെരുവിൽ അപകടകരമായ തുറന്ന മാൻഹോൾ ഉണ്ട്": ("ml", "There is a dangerous open manhole on our street."),
    "ਸਾਡੀ ਗਲੀ ਵਿੱਚ ਖਤਰਨਾਕ ਖੁੱਲ੍ਹਾ ਮੈਨਹੋਲ ਹੈ": ("pa", "There is a dangerous open manhole on our street."),
    "ہماری گلی میں خطرناک کھلا مین ہول ہے": ("ur", "There is a dangerous open manhole on our street."),
    "ଆମ ରାସ୍ତାରେ ବିପଜ୍ଜନକ ଖୋଲା ମ୍ୟାନହୋଲ ଅଛି": ("or", "There is a dangerous open manhole on our street."),
    "আমাৰ পথত বিপজ্জনক খোলা মেনহ'ল আছে": ("as", "There is a dangerous open manhole on our street."),

    # Traffic
    "আমাদের মোড়ে ট্রাফিক সিগন্যাল কাজ করছে না": ("bn", "The traffic signal at our intersection is not working."),
    "எங்கள் சந்திப்பில் போக்குவரத்து சிக்னல் வேலை செய்யவில்லை": ("ta", "The traffic signal at our intersection is not working."),
    "మా కూడలిలో ట్రాఫిక్ సిగ్నల్ పని చేయడం లేదు": ("te", "The traffic signal at our intersection is not working."),
    "आमच्या चौकातील वाहतूक सिग्नल काम करत नाही": ("mr", "The traffic signal at our intersection is not working."),
    "અમારા ચોકમાં ટ્રાફિક સિગ્નલ કામ કરતું નથી": ("gu", "The traffic signal at our intersection is not working."),
    "ನಮ್ಮ ಜಂಕ್ಷನ್‌ನಲ್ಲಿ ಟ್ರಾಫಿಕ್ ಸಿಗ್ನಲ್ ಕೆಲಸ ಮಾಡುತ್ತಿಲ್ಲ": ("kn", "The traffic signal at our intersection is not working."),
    "ഞങ്ങളുടെ ജംഗ്ഷനിലെ ട്രാഫിക് സിഗ്നൽ പ്രവർത്തിക്കുന്നില്ല": ("ml", "The traffic signal at our intersection is not working."),
    "ਸਾਡੇ ਚੌਕ ਵਿੱਚ ਟ੍ਰੈਫਿਕ ਸਿਗਨਲ ਕੰਮ ਨਹੀਂ ਕਰ ਰਿਹਾ": ("pa", "The traffic signal at our intersection is not working."),
    "ہمارے چوک میں ٹریفک سگنل کام نہیں کر رہا": ("ur", "The traffic signal at our intersection is not working."),
    "ଆମ ଛକରେ ଟ୍ରାଫିକ୍ ସିଗନାଲ୍ କାମ କରୁନାହିଁ": ("or", "The traffic signal at our intersection is not working."),
    "আমাৰ চাৰিআলিত ট্ৰেফিক ছিগনেল কাম কৰা নাই": ("as", "The traffic signal at our intersection is not working."),

    # Other (generic catch-all)
    "এই সমস্যাটি অনেক দিন ধরে সমাধান হয়নি": ("bn", "This problem has not been resolved for many days."),
    "இந்த பிரச்சனை பல நாட்களாக தீர்க்கப்படவில்லை": ("ta", "This problem has not been resolved for many days."),
    "ఈ సమస్య చాలా రోజులుగా పరిష్కారం కాలేదు": ("te", "This problem has not been resolved for many days."),
    "ही समस्या बर्‍याच दिवसांपासून सुटलेली नाही": ("mr", "This problem has not been resolved for many days."),
    "આ સમસ્યા ઘણા દિવસોથી ઉકેલાઈ નથી": ("gu", "This problem has not been resolved for many days."),
    "ಈ ಸಮಸ್ಯೆ ಹಲವು ದಿನಗಳಿಂದ ಪರಿಹಾರವಾಗಿಲ್ಲ": ("kn", "This problem has not been resolved for many days."),
    "ഈ പ്രശ്നം പല ദിവസങ്ങളായി പരിഹരിച്ചിട്ടില്ല": ("ml", "This problem has not been resolved for many days."),
    "ਇਹ ਸਮੱਸਿਆ ਕਈ ਦਿਨਾਂ ਤੋਂ ਹੱਲ ਨਹੀਂ ਹੋਈ": ("pa", "This problem has not been resolved for many days."),
    "یہ مسئلہ کئی دنوں سے حل نہیں ہوا": ("ur", "This problem has not been resolved for many days."),
    "ଏହି ସମସ୍ୟା ଅନେକ ଦିନ ଧରି ସମାଧାନ ହୋଇନାହିଁ": ("or", "This problem has not been resolved for many days."),
    "এই সমস্যাটো বহুদিন ধৰি সমাধান হোৱা নাই": ("as", "This problem has not been resolved for many days."),
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
