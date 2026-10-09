"""Shared helpers for the coverage pipeline (no third-party packages)."""
import csv
import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

STATES_50_DC = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

# Spellings that differ between the Qualtrics state dropdowns and the school CSVs.
STATE_ALIASES = {"Virgin Islands": "U.S. Virgin Islands"}

# Default state codebooks, taken from the Oct 2026 .qsf exports (RecodeValues of QID6 / QID7 / QID279).
# The student survey numbers Delaware/DC and Utah..Wyoming differently from the educator and librarian surveys.
_EDU_STATES = [
    "Alabama", "Alaska", "American Samoa", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "District of Columbia", "Delaware", "Florida", "Georgia", "Guam", "Hawaii", "Idaho", "Illinois", "Indiana",
    "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Northern Mariana Islands", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Puerto Rico", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
    "Utah", "Vermont", "Virginia", "Virgin Islands", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
_STU_STATES = [
    "Alabama", "Alaska", "American Samoa", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Guam", "Hawaii", "Idaho", "Illinois", "Indiana",
    "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Northern Mariana Islands", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Puerto Rico", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
    "U.S. Virgin Islands", "Utah", "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
DEFAULT_STATE_CODES = {
    "student": {str(i + 1): s for i, s in enumerate(_STU_STATES)},
    "educator": {str(i + 1): s for i, s in enumerate(_EDU_STATES)},
    "librarian": {str(i + 1): s for i, s in enumerate(_EDU_STATES)},
}


# --- The research team's string cleaning (ported from match_pif_nces.R / Lila's dashboard code) --------------
import unicodedata

_KEYWORDS = [
    (r"\bSCH\b$", "SCHOOL"),
    (r"\bSHS\b$", "HIGH SCHOOL"),
    (r"\bH\s*S\b$", "HIGH SCHOOL"),
    (r"\bHS\b$", "HIGH SCHOOL"),
    (r"\bHIGH\b$", "HIGH SCHOOL"),
    (r"\bSENIOR\s*HIGH\b$", "HIGH SCHOOL"),
    (r"\bSENIOR\s*HIGH\s*SCHOOL\b$", "HIGH SCHOOL"),
    (r"\bJUNIOR\s*HIGH\b$", "JUNIOR HIGH SCHOOL"),
    (r"\bJHS\b$", "JUNIOR HIGH SCHOOL"),
    (r"\bJH\b$", "JUNIOR HIGH SCHOOL"),
    (r"\bMID\b$", "MIDDLE SCHOOL"),
    (r"\bMIDDLE\b$", "MIDDLE SCHOOL"),
    (r"\bELEMEN(TARY)?\b$", "ELEMENTARY SCHOOL"),
    (r"\bEL\b$", "ELEMENTARY SCHOOL"),
    (r"\bE\s*S\b$", "ELEMENTARY SCHOOL"),
    (r"\bES\b$", "ELEMENTARY SCHOOL"),
    (r"\bM\s*S\b$", "MIDDLE SCHOOL"),
    (r"\bMS\b$", "MIDDLE SCHOOL"),
    (r"\bMIDDLE\s*AND\s*HIGH\b", "MIDDLE HIGH"),
    (r"\bMIDDLEHIGH\b", "MIDDLE HIGH"),
]
_KEYWORDS = [(re.compile(p), r) for p, r in _KEYWORDS]


def clean_string(s):
    """Same steps, same order, as the R clean_string(): upper-case, Latin-ASCII, punctuation to spaces,
    keyword expansion at the end of the string (SCH -> SCHOOL, HS -> HIGH SCHOOL, ...), squish."""
    if s is None:
        return ""
    s = str(s).upper()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s]|_", " ", s)
    for rx, rep in _KEYWORDS:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()


# State code <-> name as the R scripts spell them (note "D.C.").
STATE_CODE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DC": "District of Columbia", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "GU": "Guam",
    "MP": "Northern Mariana Islands", "VI": "U.S. Virgin Islands", "AS": "American Samoa",
}
_STATE_BY_CLEAN = {clean_string(v): k for k, v in STATE_CODE_NAMES.items()}
_STATE_BY_CLEAN.update({"D C": "DC", "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC", "NORTHERN MARIANAS": "MP",
                        "VIRGIN ISLANDS": "VI", "U S VIRGIN ISLANDS": "VI"})


def state_code_of(name_or_code):
    """'Pennsylvania' / 'PENNSYLVANIA' / 'PA' / 'D.C.' -> 'PA' / 'DC'; '' if unknown."""
    s = (name_or_code or "").strip()
    if len(s) == 2 and s.upper() in STATE_CODE_NAMES:
        return s.upper()
    return _STATE_BY_CLEAN.get(clean_string(s), "")


def norm_state(name):
    name = (name or "").strip()
    return STATE_ALIASES.get(name, name)


def load_config(path=None):
    path = path or os.path.join(HERE, "config.json")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["_path"] = path
    return cfg


def read_csv(path, encoding="utf-8-sig"):
    with open(path, encoding=encoding, newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, rows, fieldnames=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def load_state_codes(data_dir):
    """state_codes.csv (survey, code, state_name) if present, else the built-in defaults."""
    p = os.path.join(data_dir, "state_codes.csv")
    if not os.path.exists(p):
        return {k: dict(v) for k, v in DEFAULT_STATE_CODES.items()}
    out = {}
    for r in read_csv(p):
        out.setdefault(r["survey"], {})[str(r["code"])] = r["state_name"]
    return out


def parse_dt(s):
    """Qualtrics timestamps: ISO ('2026-10-01T14:03:22Z', '2026-10-01 14:03:22') or Excel-style '10/1/26 14:03'.
    Returns an aware UTC datetime or None. Qualtrics CSV times are in the account time zone; the API returns UTC."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def to_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "1.0")


def glob_match(pattern, value):
    """Shell-style match ('*direct', 'press-*', 'a|b'); empty pattern matches anything."""
    if not pattern:
        return True
    value = (value or "").lower()
    for alt in pattern.lower().split("|"):
        alt = alt.strip()
        if not alt:
            continue
        rx = "^" + re.escape(alt).replace(r"\*", ".*") + "$"
        if re.match(rx, value):
            return True
    return False
