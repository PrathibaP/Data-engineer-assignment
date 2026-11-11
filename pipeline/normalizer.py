"""Deterministic normalisation and eligibility rules (no LLM).

Used before the LLM (pre-filtering, so we never pay to extract a "Sales Manager" posting) and after
it (canonical company/title/location, salary annualisation, final US + role eligibility).
"""
from __future__ import annotations

import html
import re
import unicodedata

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- text


def html_to_text(raw: str) -> str:
    """Greenhouse returns HTML *entity-escaped* inside JSON, so unescape before parsing."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    soup = BeautifulSoup(raw, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        br.insert_before("\n")
    text = soup.get_text(" ")
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# ---------------------------------------------------------------- company

_COMPANY_SUFFIX = re.compile(r"[,\s]+(inc\.?|llc|ltd\.?|corp\.?|corporation|co\.?|technologies|labs|hq|gmbh)\s*$", re.I)
COMPANY_ALIASES = {
    "stripe": "Stripe", "airbnb": "Airbnb", "databricks": "Databricks", "figma": "Figma",
    "palantir": "Palantir", "palantir technologies": "Palantir", "plaid": "Plaid",
}


def company_key(name: str | None) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKC", name).strip()
    n = re.sub(r"\(.*?\)", "", n)
    prev = None
    while prev != n:
        prev, n = n, _COMPANY_SUFFIX.sub("", n).strip()
    n = re.sub(r"\.(com|ai|io|co)$", "", n.lower())
    return re.sub(r"[^a-z0-9]+", "", n)


def normalize_company(name: str | None) -> str | None:
    if not name:
        return None
    key = company_key(name)
    for alias, canon in COMPANY_ALIASES.items():
        if company_key(alias) == key:
            return canon
    clean = _COMPANY_SUFFIX.sub("", name.strip()).strip(" ,")
    return clean or None


# ---------------------------------------------------------------- titles

_TITLE_SUBS = [
    (r"\bsr\b\.?", "senior"), (r"\bsnr\b\.?", "senior"), (r"\bjr\b\.?", "junior"),
    (r"\bml\b", "machine learning"), (r"\bai/ml\b", "ai machine learning"), (r"\bmle\b", "machine learning engineer"),
    (r"\beng\b\.?", "engineer"), (r"\bengr\b\.?", "engineer"), (r"\bswe\b", "software engineer"),
    (r"\bmgr\b", "manager"), (r"\bprin\b\.?", "principal"),
    (r"\biii\b", "3"), (r"\bii\b", "2"), (r"\biv\b", "4"),
]
SENIORITY_WORDS = {"senior", "junior", "staff", "principal", "lead", "distinguished", "associate", "entry",
                   "level", "mid", "new", "grad", "1", "2", "3", "4", "i"}


def normalize_title(title: str) -> str:
    """Lower-cased, abbreviation-expanded title, e.g. 'Sr. ML Eng (Infra)' -> 'senior machine learning engineer infra'."""
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = t.replace("–", "-").replace("—", "-")
    for pat, rep in _TITLE_SUBS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^a-z0-9+ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_core(title: str) -> str:
    """The role family without seniority and without trailing team qualifiers.

    'Senior Data Engineer, Platform' -> 'data engineer'; 'Staff ML Engineer - Ads' -> 'machine learning engineer'.
    """
    t = re.split(r"\s[-,|(/]\s?|,", (title or "").replace("–", "-").replace("—", "-"), maxsplit=1)[0]
    words = [w for w in normalize_title(t).split() if w not in SENIORITY_WORDS]
    return " ".join(words)


def title_key(title: str) -> str:
    return normalize_title(title)


def clean_title(title: str) -> str:
    """Human-readable canonical title: expand abbreviations, keep original casing style."""
    t = (title or "").strip()
    t = re.sub(r"\bSr\.?(?=\s)", "Senior", t)
    t = re.sub(r"\bJr\.?(?=\s)", "Junior", t)
    t = re.sub(r"\bML\b", "Machine Learning", t)
    t = re.sub(r"\bEng\.?(?=$|\s|,)", "Engineer", t)
    t = re.sub(r"\s+", " ", t.replace("–", "-").replace("—", "-"))
    return t.strip(" -|,")


def seniority_from_title(title: str) -> str | None:
    t = normalize_title(title)
    if re.search(r"\b(staff|principal|distinguished|fellow|head of)\b", t):
        return "staff_plus"
    if re.search(r"\b(senior|lead|3)\b", t):
        return "senior"
    if re.search(r"\b(junior|associate|entry|new grad|graduate|intern|1)\b", t):
        return "junior"
    if re.search(r"\b(2|mid)\b", t):
        return "mid"
    return None


# ---------------------------------------------------------------- role eligibility

ROLE_PATTERNS = [
    r"\bdata (platform |infrastructure |infra |integration |pipeline )?engineer",
    r"\bmachine learning (platform |infrastructure |infra |ops |systems )?engineer",
    r"\b(ai|mlops|ml ops|llm|ai machine learning) (platform |infrastructure )?engineer",
    r"\banalytics engineer",
    r"\bbig data engineer",
    r"\betl (developer|engineer)",
    r"\bdata warehouse engineer",
    r"\bplatform engineer",  # needs data/ML context, see below
    r"\b(data|ml|machine learning) (software )?engineer",
]
EXCLUDE_PATTERNS = [
    r"\b(manager|director|head|vp|vice president|recruiter|sales|account executive|marketing|designer|"
    r"scientist|analyst|counsel|solutions architect|support|product manager|technical program)\b",
]
DATA_CONTEXT = re.compile(
    r"\b(data|machine learning|ml|spark|kafka|airflow|dbt|flink|warehouse|lakehouse|snowflake|bigquery|"
    r"etl|feature store|model (training|serving)|inference|gpu|pytorch|tensorflow)\b", re.I)


def is_target_title(title: str, description: str = "") -> bool:
    """True if the title is a data/ML/analytics/platform engineering role.

    A bare 'Platform Engineer' (or 'Software Engineer, Infrastructure') is only accepted when the title or the
    description shows data/ML context, so we do not accept a role on one keyword alone.
    """
    t = normalize_title(title)
    if not t or any(re.search(p, t) for p in EXCLUDE_PATTERNS):
        # 'Engineering Manager, Data Platform' -> excluded (not an engineer IC role)
        return False
    for p in ROLE_PATTERNS:
        if re.search(p, t):
            if "platform engineer" in p:
                ctx = len(DATA_CONTEXT.findall(t + " " + (description or "")[:4000]))
                return bool(re.search(r"\b(data|machine learning|ai|ml)\b", t)) or ctx >= 3
            return True
    return False


# ---------------------------------------------------------------- location

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
US_CITIES = {
    "san francisco", "sf", "new york", "nyc", "new york city", "seattle", "austin", "boston", "chicago",
    "los angeles", "denver", "mountain view", "palo alto", "menlo park", "sunnyvale", "san jose", "oakland",
    "washington", "atlanta", "miami", "san diego", "portland", "pittsburgh", "philadelphia", "salt lake city",
    "bellevue", "redwood city", "cambridge", "brooklyn", "south san francisco", "san mateo", "dallas", "houston",
}
CITY_ALIASES = {"sf": "San Francisco", "nyc": "New York", "new york city": "New York", "sfo": "San Francisco",
                "bay area": "San Francisco", "la": "Los Angeles"}
NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|london|uk|united kingdom|ireland|dublin|germany|berlin|munich|"
    r"france|paris|netherlands|amsterdam|india|bangalore|bengaluru|hyderabad|singapore|japan|tokyo|"
    r"australia|sydney|brazil|sao paulo|mexico|spain|madrid|poland|warsaw|israel|tel aviv|emea|apac|"
    r"europe|latam|switzerland|zurich|sweden|stockholm|denmark|copenhagen|korea|seoul)\b", re.I)
_US_HINT_CI = re.compile(r"\b(united states|usa|u\.s\.a?\.?|us-remote|remote[- ]us|north america)\b", re.I)
_US_HINT_CS = re.compile(r"\b(US|USA|U\.S\.)\b")  # case-sensitive so "join us" doesn't count


class _USHint:
    @staticmethod
    def search(s: str):
        return _US_HINT_CI.search(s) or _US_HINT_CS.search(s)


US_HINT = _USHint()


def normalize_city(city: str | None) -> str | None:
    if not city:
        return None
    c = city.strip().strip(",")
    return CITY_ALIASES.get(c.lower(), c.title() if c.islower() or c.isupper() else c)


def normalize_country(country: str | None) -> str | None:
    if not country:
        return None
    c = country.strip().lower().replace(".", "")
    if c in {"us", "usa", "united states", "united states of america", "america", "u s"}:
        return "US"
    if len(c) == 2:
        return c.upper()
    return country.strip().title()


def looks_us(location_text: str | None) -> bool | None:
    """True = clearly US, False = clearly non-US only, None = unknown. Used for pre-filtering."""
    if not location_text:
        return None
    loc = location_text
    has_us = bool(US_HINT.search(loc)) or any(re.search(rf"\b{re.escape(c)}\b", loc.lower()) for c in US_CITIES) \
        or bool(re.search(r",\s*(%s)\b" % "|".join(US_STATES), loc))
    has_non_us = bool(NON_US.search(loc))
    if has_us:
        return True
    if has_non_us:
        return False
    return None


# ---------------------------------------------------------------- salary

PERIOD_FACTOR = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}


def annualize(amount: float | None, period: str | None) -> float | None:
    """Annualise only when the period is known. A missing period with a value >= 10,000 is treated as
    annual (US convention for '$150k-$200k'); smaller numbers with no period are rejected as ambiguous."""
    if amount is None:
        return None
    if period:
        return round(amount * PERIOD_FACTOR[period], 2)
    return amount if amount >= 10_000 else None
