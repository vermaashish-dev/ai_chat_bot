"""IndiaMART paid-service catalogue and a turnover-based package suggestion.

Catalogue facts are copied from the official help page (SOURCE_URL, read 2026-10-10). Prices are as
listed there; the page does not say whether GST is included, so the agent says the executive confirms
the final price. The turnover -> package mapping is OUR heuristic (the page sets no eligibility rules),
so every suggestion is labelled as a suggestion with its reason.

Every figure the voice agent may speak is produced here already in words ("thirty five thousand
rupees"), because on live calls the LLM misread digits (it said fifteen thousand for 35,000).
"""

SOURCE_URL = "https://help.indiamart.com/knowledge-base/paid-services-of-indiamart/"
SOURCE_READ_ON = "2026-10-10"
CUSTOMER_CARE_PHONE = "9696969696"
CUSTOMER_CARE_EMAIL = "customercare@indiamart.com"

CATALOG: dict[str, dict] = {
    "MDC": {
        "name": "Mini Dynamic Catalog (MDC)",
        "prices": {1: 35000, 2: 72000, 3: 90000},
        "buyleads": (10, 1, None),
        "benefits": ["listing stays visible online round the clock", "new buyers every day", "lead management system"],
    },
    "TrustSEAL Pro": {
        "name": "TrustSEAL Pro",
        "prices": {1: 60000, 2: 90000, 3: 120000},
        "buyleads": (14, 1, None),
        "benefits": ["verified TrustSEAL certificate for credibility", "more visibility and enquiries", "new buyers every day"],
    },
    "Maximiser Pro": {
        "name": "Maximiser Pro",
        "prices": {1: 85000, 2: 130000, 3: 170000},
        "buyleads": (28, 1, None),
        "benefits": ["own domain connected to the B2B marketplace", "customisable website templates", "product images and videos"],
    },
    "IM Star Pro": {
        "name": "IM Star Pro",
        "prices": {},
        "buyleads": (30, 1, 6),
        "benefits": ["higher visibility across buyer searches", "Star Supplier label"],
    },
    "IM Leader Pro": {
        "name": "IM Leader Pro",
        "prices": {},
        "buyleads": (40, 1, 7),
        "benefits": ["higher visibility across buyer searches", "Leading Supplier label"],
    },
    "IM Industry Leader": {
        "name": "IM Industry Leader",
        "prices": {},
        "buyleads": None,  # grows with categories, no fixed count listed
        "benefits": ["prime spot in the chosen category and location", "Industry Leader label on hero products"],
    },
}

# annual_turnover band -> (package key, why). Heuristic: package cost scales with business size.
_BY_TURNOVER = {
    "0 - 40 L": ("MDC", "entry package suited to a small business: lowest listed price with daily buyer visibility"),
    "40 L - 1.5 Cr": ("TrustSEAL Pro", "growing business: a verified TrustSEAL adds credibility with new buyers"),
    "1.5 - 5 Cr": ("Maximiser Pro", "established business: own-domain website and more buyleads"),
    "5 - 25 Cr": ("IM Star Pro", "larger business: higher visibility and Star Supplier label"),
    "25 - 100 Cr": ("IM Leader Pro", "large business: top visibility and Leading Supplier label"),
    "100 - 500 Cr": ("IM Industry Leader", "very large business: prime category placement"),
    "> 500 Cr": ("IM Industry Leader", "very large business: prime category placement"),
}

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _below_hundred(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def in_words(n: int) -> str:
    """Indian numbering in words: 120000 -> 'one lakh twenty thousand'."""
    if n == 0:
        return "zero"
    parts = []
    for size, label in ((10_000_000, "crore"), (100_000, "lakh"), (1_000, "thousand"), (100, "hundred")):
        if n >= size:
            parts.append(f"{in_words(n // size) if size == 10_000_000 else _below_hundred(n // size)} {label}")
            n %= size
    if n:
        parts.append(_below_hundred(n))
    return " ".join(parts)


def digits_in_words(number: str) -> str:
    """Phone numbers are read digit by digit: '96' -> 'nine six'."""
    return " ".join(_DIGITS[int(d)] for d in number if d.isdigit())


def _years(y: int) -> str:
    return "one year" if y == 1 else f"{in_words(y)} years"


def price_spoken(key: str) -> str:
    p = CATALOG[key]["prices"]
    if not p:
        return "price on quote from the sales executive"
    return ", ".join(f"{in_words(v)} rupees for {_years(y)}" for y, v in sorted(p.items()))


def price_line(key: str) -> str:
    """Digits, for reports and specs (not spoken)."""
    p = CATALOG[key]["prices"]
    if not p:
        return "price on quote from the sales executive"
    return ", ".join(f"Rs {v:,} for {y} year{'s' if y > 1 else ''}" for y, v in sorted(p.items())) + " as listed by IndiaMART"


def buyleads_spoken(key: str) -> str:
    b = CATALOG[key]["buyleads"]
    if b is None:
        return "buyleads that grow with the categories chosen"
    weekly, daily, per_cat = b
    text = f"{in_words(weekly)} weekly buyleads plus {in_words(daily)} daily bonus"
    return text + (f", {in_words(per_cat)} weekly per category" if per_cat else "")


def spoken_line(key: str) -> str:
    item = CATALOG[key]
    return f"{item['name']}: {price_spoken(key)}; {buyleads_spoken(key)}; {', '.join(item['benefits'])}"


def basic_package_spoken() -> str:
    return "the most basic paid package is " + spoken_line("MDC")


def customer_care_spoken() -> str:
    return digits_in_words(CUSTOMER_CARE_PHONE)


def recommend(annual_turnover: str | None, enquiries_90d: float | None = None) -> dict:
    """Suggest one package from the turnover band; unknown turnover falls back to the entry package."""
    if annual_turnover in _BY_TURNOVER:
        key, why = _BY_TURNOVER[annual_turnover]
        confidence = "medium"
    else:
        key, why = "MDC", "turnover not on record, so the entry package is suggested"
        confidence = "low"
    item = CATALOG[key]
    keys = list(CATALOG)
    i = keys.index(key)
    alternative = keys[i - 1] if i > 0 else keys[1]
    reason = f"Annual turnover {annual_turnover or 'unknown'}: {why}."
    if enquiries_90d:
        reason += f" The seller already received {int(enquiries_90d)} enquiries in 90 days, so more buyleads build on existing demand."
    return {
        "key": key,
        "package": item["name"],
        "price": price_line(key),
        "price_spoken": price_spoken(key),
        "buyleads": buyleads_spoken(key),
        "benefits": item["benefits"],
        "alternative": CATALOG[alternative]["name"],
        "reason": reason,
        "confidence": confidence,
        "basis": "Suggestion from turnover band (heuristic); catalogue facts from " + SOURCE_URL,
    }


def catalog_names() -> str:
    """Names and one benefit each, no prices: other packages are priced by the sales executive."""
    return "; ".join(f"{v['name']}: {v['benefits'][0]}" for v in CATALOG.values())
