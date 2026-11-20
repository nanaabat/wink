from __future__ import annotations

import re

_BANNED = [
    r"\b(detect|detects|detected|detecting|read|reads|reading|sense|senses|sensing|recogni[sz]e[sd]?)\b[^.]{0,30}\b(your|user'?s?|people'?s?|their|his|her|a person'?s?|occupants?'?)\b[^.]{0,20}\b(emotion|emotions|mood|moods|feelings?)\b",
    r"\bemotion (detection|recognition)\b",
    r"\byou (feel|are feeling|seem|look) (anxious|stressed|sad|angry|calm|happy|uneasy|tired)\b",
    r"\b(we|hunch|wink) (know|knows) how you feel\b",
]


class EmotionClaimError(ValueError):
    pass


def assert_no_emotion_claims(text: str) -> str:
    low = text.lower()
    for pat in _BANNED:
        if re.search(pat, low):
            raise EmotionClaimError(f"unsupported emotional-detection claim in: {text!r}")
    return text
