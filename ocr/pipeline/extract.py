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
# 세부 코드는 두 자리까지 (F-1-13, F-2-99). 뒤에 숫자·하이픈이 더 붙으면 잘라 쓰지 않는다
VISA = re.compile(r"([A-Z])\s*[-–—]\s*(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?(?![-–—\d])")
UPPER_LINE = re.compile(r"^[A-Z][A-Z .,'-]+$")
LABELS = {"KOR", "RESIDENCECARD", "REGISTRATIONNO", "NAME", "NARNE", "STATUS"}

# 필드별 라벨. 다른 필드 라벨을 만나면 탐색을 멈추는 데 쓴다
NAME_KEYWORDS = ["성명", "Name", "Narne"]
COUNTRY_KEYWORDS = ["국가", "Country"]
STATUS_KEYWORDS = ["체류자격", "Status"]
ISSUE_KEYWORDS = ["발급", "Issue"]
NAME_LABEL = re.compile("|".join(NAME_KEYWORDS))


def _field(name, value=None, confidence=None):
    conf = None if confidence is None else round(float(confidence), 2)
    return {"name": name, "value": value, "confidence": conf}


def _find_anchor(lines, keywords):
    for i, (text, _) in enumerate(lines):
        if any(k in text for k in keywords):
            return i
    return None


def _is_value_line(text):
    return bool(UPPER_LINE.match(text)) and text.replace(" ", "") not in LABELS


def _next_upper_line(lines, start, stop, look=3):
    """start 다음 줄부터 영문 대문자 줄을 찾는다. 다른 필드 라벨(stop)을 만나면 멈춘다"""
    for text, score in lines[start + 1:start + 1 + look]:
        t = text.strip()
        if any(k in t for k in stop):
            break  # 다음 필드로 넘어갔으면 그 값을 가져오지 않는다
        if _is_value_line(t):
            return t, score
    return None, None


def _name_en(lines):
    i = _find_anchor(lines, NAME_KEYWORDS)
    if i is None:
        return _field("NAME_EN")
    # 라벨과 이름이 한 줄로 읽힌 경우 ("성명 Name SAMPLE PERSON")
    text, score = lines[i]
    rest = NAME_LABEL.sub("", text).strip()
    if _is_value_line(rest):
        return _field("NAME_EN", rest, score)
    text, score = _next_upper_line(lines, i, COUNTRY_KEYWORDS + STATUS_KEYWORDS + ISSUE_KEYWORDS)
    return _field("NAME_EN", text, score)

def _country_code(text):
    """국가 줄 → 국가 코드.
    정확히 일치하면 바로 쓰고, 아니면 국가 이름이 단어로 들어 있는지 찾는다.
    예: 'REPUBLIC OF THE PHILIPPINES' → 'PHILIPPINES' → PHL
    """
    text = re.sub(r"\s+", " ", text).strip()
    if text in COUNTRY_CODES:
        return COUNTRY_CODES[text]
    # 긴 이름부터 본다. 'VIET NAM'처럼 여러 단어인 이름이 먼저 걸리게
    for name in sorted(COUNTRY_CODES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text):
            return COUNTRY_CODES[name]
    return None

def _nationality(lines):
    i = _find_anchor(lines, COUNTRY_KEYWORDS)
    if i is None:
        return _field("NATIONALITY")
    # 라벨과 값이 한 줄로 붙어 나오는 경우 (예: '국가/지역 REPUBLIC OF THE PHILIPPINES')
    label_text, label_score = lines[i]
    code = _country_code(label_text)
    if code:
        return _field("NATIONALITY", code, label_score)
    # 라벨 다음 줄에 값이 있는 경우
    text, score = _next_upper_line(lines, i, STATUS_KEYWORDS + ISSUE_KEYWORDS)
    if text is None:
        return _field("NATIONALITY")
    code = _country_code(text)
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