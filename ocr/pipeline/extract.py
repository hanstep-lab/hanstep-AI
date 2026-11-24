import re
from datetime import date

from .country import find_country

FIELD_NAMES = [
    "NAME_EN",
    "NATIONALITY",
    "BIRTH_DATE",
    "VISA_TYPE",
    "STAY_EXPIRES_ON",
    "REGISTRATION_NO",
]

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
# 국가 칸 라벨 글자. 국가명 후보에서 지운다 ('국가/지역 REPUBLIC OF ...', 'Country/Region')
COUNTRY_LABEL = re.compile("국가|지역|Country|Region")
COUNTRY_LOOK = 3  # 라벨 다음 몇 줄까지 국가명을 찾는지


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

def _nationality(lines):
    i = _find_anchor(lines, COUNTRY_KEYWORDS)
    if i is None:
        return _field("NATIONALITY")
    # 후보: 라벨 줄 자체('국가/지역 REPUBLIC OF ...'처럼 한 줄로 읽힌 경우) + 다음 줄들
    candidates = [lines[i]]
    for text, score in lines[i + 1:i + 1 + COUNTRY_LOOK]:
        if any(k in text for k in STATUS_KEYWORDS + ISSUE_KEYWORDS):
            break  # 다음 필드로 넘어갔으면 그 값을 가져오지 않는다
        candidates.append((text, score))
    found = find_country([(COUNTRY_LABEL.sub(" ", t), s) for t, s in candidates])
    # 못 찾았거나 지원 범위 밖 나라면 값 없이 보내서 사용자 확인으로 넘긴다
    if found is None or found.code is None:
        return _field("NATIONALITY")
    # 글자를 고치거나 비슷한 이름으로 찾았으면 사용자 확인으로 넘긴다
    confidence = found.score if found.sure else min(found.score, SUSPECT_CONFIDENCE)
    return _field("NATIONALITY", found.code, confidence)


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


# 값 검증. 규칙에 안 맞으면 값은 두고 신뢰도만 낮춰 사용자 확인으로 넘긴다 (서버는 0.8 미만이면 needsReview)
SUSPECT_CONFIDENCE = 0.5
FOREIGNER_GENDER_DIGITS = {"5", "6", "7", "8"}  # 외국인등록번호 7번째 숫자: 5·7 남성, 6·8 여성
MIN_AGE = 15   # 임시값: 등록증 소지자 최소 나이
MAX_AGE = 100  # 임시값: 이보다 많으면 오인식으로 본다
VISA_CLASSES = set("ABCDEFGH")  # 체류자격 대분류 첫 글자


def _suspect(field):
    """신뢰도를 SUSPECT_CONFIDENCE 이하로 낮춘다. 값이 없는 칸은 건드리지 않는다"""
    if field["value"] is not None and field["confidence"] is not None:
        field["confidence"] = min(field["confidence"], SUSPECT_CONFIDENCE)


def _age(birth, today):
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _validate(found, today):
    """규칙에 안 맞는 필드의 신뢰도를 낮춘다. found를 직접 고친다"""
    reg, birth = found["REGISTRATION_NO"], found["BIRTH_DATE"]

    # 1. 등록번호 7번째 숫자가 5·6·7·8이 아니면 내국인 번호거나 오인식 → 등록번호·생년월일 둘 다 의심
    if reg["value"] is not None and reg["value"][7] not in FOREIGNER_GENDER_DIGITS:
        _suspect(reg)
        _suspect(birth)

    # 2. 생년월일이 미래거나 만 MIN_AGE세 미만, 만 MAX_AGE세 초과면 의심
    if birth["value"] is not None:
        born = date.fromisoformat(birth["value"])
        if born > today or not MIN_AGE <= _age(born, today) <= MAX_AGE:
            _suspect(birth)

    # 3. 체류자격 첫 글자가 A~H가 아니면 의심
    visa = found["VISA_TYPE"]
    if visa["value"] is not None and visa["value"][0] not in VISA_CLASSES:
        _suspect(visa)


def extract_fields(lines, today=None):
    """lines: [(인식된 글자, 신뢰도), ...] → 계약 형식 fields 배열
    today: 생년월일 검증 기준 날짜 (테스트용). 없으면 오늘
    """
    reg, birth = _registration_and_birth(lines)
    found = {
        "NAME_EN": _name_en(lines),
        "NATIONALITY": _nationality(lines),
        "BIRTH_DATE": birth,
        "VISA_TYPE": _visa_type(lines),
        "STAY_EXPIRES_ON": _field("STAY_EXPIRES_ON"),  # 뒷면 인식은 v0-lite 범위 밖
        "REGISTRATION_NO": reg,
    }
    _validate(found, today or date.today())
    return [found[n] for n in FIELD_NAMES]