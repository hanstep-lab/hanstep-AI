import re
from datetime import date

FIELD_NAMES = [
    "NAME_EN",
    "NATIONALITY",
    "BIRTH_DATE",
    "VISA_TYPE",
    "STAY_EXPIRES_ON",
    "REGISTRATION_NO",
]

# E-9 송출국 위주. 등록증 표기 → ISO 3166-1 alpha-3
COUNTRY_CODES = {
    "VIETNAM": "VNM", "VIET NAM": "VNM",
    "PHILIPPINES": "PHL", "INDONESIA": "IDN", "THAILAND": "THA",
    "CAMBODIA": "KHM", "NEPAL": "NPL", "MYANMAR": "MMR",
    "SRI LANKA": "LKA", "UZBEKISTAN": "UZB", "BANGLADESH": "BGD",
    "MONGOLIA": "MNG", "PAKISTAN": "PAK", "KYRGYZSTAN": "KGZ",
    "TIMOR-LESTE": "TLS", "EAST TIMOR": "TLS", "LAOS": "LAO",
    "CHINA": "CHN",
}

REG_NO = re.compile(r"(\d{6})\s*[-–—~]?\s*(\d)(\d{6})")
VISA = re.compile(r"([A-Z])\s*[-–—]\s*(\d{1,2})(?:\s*[-–—]\s*(\d))?")
UPPER_LINE = re.compile(r"^[A-Z][A-Z .,'-]+$")
LABELS = {"KOR", "RESIDENCECARD", "REGISTRATIONNO", "NAME", "NARNE", "STATUS"}


def _field(name, value=None, confidence=None):
    conf = None if confidence is None else round(float(confidence), 2)
    return {"name": name, "value": value, "confidence": conf}


def _find_anchor(lines, keywords):
    for i, (text, _) in enumerate(lines):
        if any(k in text for k in keywords):
            return i
    return None


def _next_upper_line(lines, start, look=3):
    for text, score in lines[start + 1:start + 1 + look]:
        t = text.strip()
        if UPPER_LINE.match(t) and t.replace(" ", "") not in LABELS:
            return t, score
    return None, None


def _name_en(lines):
    i = _find_anchor(lines, ["성명", "Name", "Narne"])
    if i is None:
        return _field("NAME_EN")
    text, score = _next_upper_line(lines, i)
    return _field("NAME_EN", text, score)


def _nationality(lines):
    i = _find_anchor(lines, ["국가", "Country"])
    if i is None:
        return _field("NATIONALITY")
    text, score = _next_upper_line(lines, i)
    if text is None:
        return _field("NATIONALITY")
    code = COUNTRY_CODES.get(re.sub(r"\s+", " ", text))
    # 사전에 없는 국가는 값 없이 보내서 사용자 확인으로 넘긴다
    return _field("NATIONALITY", code, score) if code else _field("NATIONALITY")


def _registration_and_birth(lines):
    for text, score in lines:
        m = REG_NO.search(text)
        if not m:
            continue
        front, gender_digit = m.group(1), m.group(2)
        masked = f"{front}-{gender_digit}******"
        century = {"1": 1900, "2": 1900, "5": 1900, "6": 1900,
                   "3": 2000, "4": 2000, "7": 2000, "8": 2000}.get(gender_digit)
        birth = None
        if century:
            try:
                birth = date(century + int(front[:2]), int(front[2:4]), int(front[4:6])).isoformat()
            except ValueError:
                birth = None  # 없는 날짜면 오인식으로 보고 비워둔다
        return (
            _field("REGISTRATION_NO", masked, score),
            _field("BIRTH_DATE", birth, score if birth else None),
        )
    return _field("REGISTRATION_NO"), _field("BIRTH_DATE")


def _visa_type(lines):
    for text, score in lines:
        m = VISA.search(text)
        if m:
            value = f"{m.group(1)}-{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
            return _field("VISA_TYPE", value, score)
    return _field("VISA_TYPE")


def extract_fields(lines):
    """lines: [(인식된 글자, 신뢰도), ...] → 계약 형식 fields 배열"""
    reg, birth = _registration_and_birth(lines)
    found = {
        "NAME_EN": _name_en(lines),
        "NATIONALITY": _nationality(lines),
        "BIRTH_DATE": birth,
        "VISA_TYPE": _visa_type(lines),
        "STAY_EXPIRES_ON": _field("STAY_EXPIRES_ON"),  # 뒷면 인식은 v0-lite 범위 밖
        "REGISTRATION_NO": reg,
    }
    return [found[n] for n in FIELD_NAMES]