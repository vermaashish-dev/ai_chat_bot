"""Live-adaptation signals (PS04 §3.5). Hinglish cue lexicon taken from seller turns in ps07_call_turns.csv.

Used for: the past-call evidence (which bot response worked after each signal), the /signals/detect
endpoint, and the cue lists placed in the Sarvam prompt. The live agent detects signals itself from
the conversation; this lexicon is a transparent, testable reference for the same signals.
"""

import re

SIGNAL_CUES: dict[str, list[str]] = {
    "rush": ["busy", "jaldi", "baad me", "baad mein", "time nahi", "abhi nahi", "driving", "meeting me", "later", "short me", "jaldi bolo"],
    "confusion": ["samajh nahi", "samjha nahi", "kya bol", "kya matlab", "matlab", "kaun bol", "kya hai ye", "clear nahi", "phir se", "dobara bolo"],
    "frustration": ["pareshan", "baar baar", "roz", "daily", "call mat", "mat karo", "band karo", "irritate", "faltu", "bakwas", "disturb"],
    "interest": ["kitne ka", "price", "kitna", "charges", "rate", "details bhejo", "haan bataiye", "interested", "kaise", "kya milega", "benefit"],
    "language_switch": ["english", "speak in", "hindi nahi", "gujarati", "marathi", "tamil", "telugu", "kannada", "bangla", "bengali", "punjabi"],
    "bot_suspicion": ["robot", "bot", "computer", "recording", "machine", "ai ho", "insaan"],
}
# Explicit do-not-call requests override every other signal.
OPT_OUT_CUES = ["call mat karo", "call mat karna", "dobara call mat", "number hata", "do not call", "don't call", "remove my number", "call band karo"]

_PATTERNS = {k: re.compile(r"\b(?:" + "|".join(map(re.escape, v)) + r")\b", re.I) for k, v in SIGNAL_CUES.items()}
_OPT_OUT = re.compile(r"\b(?:" + "|".join(map(re.escape, OPT_OUT_CUES)) + r")\b", re.I)
# Non-Latin scripts mean the seller has switched away from romanised Hindi/English.
_SCRIPTS = {
    "Gujarati": r"[઀-૿]", "Bengali": r"[ঀ-৿]", "Tamil": r"[஀-௿]", "Telugu": r"[ఀ-౿]",
    "Kannada": r"[ಀ-೿]", "Malayalam": r"[ഀ-ൿ]", "Punjabi": r"[਀-੿]", "Odia": r"[଀-୿]",
}


def detect_signals(text: str) -> dict:
    found = {k: sorted({m.group(0).lower() for m in p.finditer(text)}) for k, p in _PATTERNS.items()}
    found = {k: v for k, v in found.items() if v}
    scripts = [lang for lang, pat in _SCRIPTS.items() if re.search(pat, text)]
    if scripts:
        found.setdefault("language_switch", []).extend(f"script:{s}" for s in scripts)
    return {"signals": sorted(found), "cues": found, "opt_out": bool(_OPT_OUT.search(text))}


def matches(signal: str, text: str) -> bool:
    return bool(_PATTERNS[signal].search(text or ""))
