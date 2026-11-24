"""국적 글자 → ISO 3166-1 alpha-3. 확실하지 않으면 틀린 코드보다 null을 낸다.

순서: ① 글자 정리 → ② 정확히 일치 → ③ 단어로 들어 있는지 → ④ 비슷한 이름(difflib).
②가 아니거나 글자를 고쳐서 맞췄으면 sure=False로 돌려서 호출하는 쪽이 신뢰도를 낮추게 한다.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# 실제(견본 합성) 카드 사진에서 확인한 표기. 11/24 사진 19장, PHL만
CARD_NAMES = {
    "REPUBLIC OF THE PHILIPPINES": "PHL",
}

# ISO·UN 명칭 기반 후보. 실제 카드 표기는 아직 확인 안 함
# E-9 송출국 18개국: 고용노동부 2026-08-14 보도자료(카자흐스탄 18번째 지정) 기준
STANDARD_NAMES = {
    "PHILIPPINES": "PHL",
    "VIETNAM": "VNM", "VIET NAM": "VNM",
    "SOCIALIST REPUBLIC OF VIET NAM": "VNM", "SOCIALIST REPUBLIC OF VIETNAM": "VNM",
    "INDONESIA": "IDN", "REPUBLIC OF INDONESIA": "IDN",
    "THAILAND": "THA", "KINGDOM OF THAILAND": "THA",
    "CAMBODIA": "KHM", "KINGDOM OF CAMBODIA": "KHM",
    "NEPAL": "NPL", "FEDERAL DEMOCRATIC REPUBLIC OF NEPAL": "NPL",
    "MYANMAR": "MMR", "REPUBLIC OF THE UNION OF MYANMAR": "MMR",
    "SRI LANKA": "LKA", "DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA": "LKA",
    "UZBEKISTAN": "UZB", "REPUBLIC OF UZBEKISTAN": "UZB",
    "BANGLADESH": "BGD", "PEOPLE'S REPUBLIC OF BANGLADESH": "BGD",
    "MONGOLIA": "MNG",
    "PAKISTAN": "PAK", "ISLAMIC REPUBLIC OF PAKISTAN": "PAK",
    "KYRGYZSTAN": "KGZ", "KYRGYZ REPUBLIC": "KGZ",
    "TIMOR-LESTE": "TLS", "EAST TIMOR": "TLS", "DEMOCRATIC REPUBLIC OF TIMOR-LESTE": "TLS",
    "LAOS": "LAO", "LAO PDR": "LAO", "LAO PEOPLE'S DEMOCRATIC REPUBLIC": "LAO",
    "CHINA": "CHN", "PEOPLE'S REPUBLIC OF CHINA": "CHN",
    "TAJIKISTAN": "TJK", "REPUBLIC OF TAJIKISTAN": "TJK",
    "KAZAKHSTAN": "KAZ", "REPUBLIC OF KAZAKHSTAN": "KAZ",
}

# 지원 범위 밖인데 비슷한 이름의 지원 국가로 잘못 바뀔 수 있는 나라. 찾으면 null로 확정한다
# (대만: ISO는 TWN이지만 E-9 송출국이 아니고, CHINA가 들어 있어 CHN으로 바뀌면 안 된다)
UNSUPPORTED_NAMES = ["REPUBLIC OF CHINA", "TAIWAN"]

# 비슷한 이름으로 인정하는 최소 점수 (0~1). 11/24 측정: 0.85면 실제 사진 04_dark_a('PHILPPIHE', 0.80)를 놓치고,
# 0.80에서도 목록 밖 국가 44개에 잘못된 코드 0건. 찾은 값은 어차피 신뢰도 0.5로 사용자 확인을 거친다
FUZZY_MIN = 0.80
FUZZY_MARGIN = 0.05  # 임시값: 1등이 다른 나라 2등보다 이만큼은 높아야 채택
MIN_LETTERS = 4     # 이보다 글자가 적은 줄은 잡음('I', '7')으로 보고 후보에서 뺀다
# 공식 이름 앞뒤에 붙는 일반 단어. 비슷한 이름은 나머지 핵심 단어만으로 비교한다
# (전체로 비교하면 'REPUBLIC OF INDIA'가 'REPUBLIC OF INDONESIA'와 비슷하게 나온다)
GENERIC_WORDS = {"REPUBLIC", "OF", "THE", "PEOPLES", "DEMOCRATIC", "SOCIALIST",
                 "KINGDOM", "FEDERAL", "ISLAMIC", "UNION", "PDR"}
# OCR이 글자를 숫자로 잘못 읽는 흔한 경우. 국가명에는 숫자가 없으므로 글자로 되돌린다
DIGIT_TO_LETTER = str.maketrans({"0": "O", "1": "I", "5": "S"})


@dataclass(frozen=True)
class Match:
    code: object   # alpha-3 문자열, 또는 None(지원 범위 밖으로 확정)
    sure: bool     # False면 사용자 확인으로 넘긴다
    score: float   # OCR 신뢰도 (두 줄을 합쳤으면 낮은 쪽)


def normalize(text):
    """→ (정리된 글자, 글자를 고쳤는지). 등록증 국가명은 대문자라 소문자·숫자는 오인식으로 본다"""
    corrected = bool(re.search(r"[a-z0-9]", text))
    t = text.replace("l", "I").upper().translate(DIGIT_TO_LETTER)  # 소문자 l은 대부분 I 오인식
    t = t.replace("'", "")                                         # PEOPLE'S → PEOPLES
    return re.sub(r"[^A-Z]+", " ", t).strip(), corrected           # 하이픈·점·한글 → 공백


def _core(text):
    return " ".join(w for w in text.split() if w not in GENERIC_WORDS)


NAMES = {normalize(n)[0]: code for n, code in {**STANDARD_NAMES, **CARD_NAMES}.items()}
NAMES.update({normalize(n)[0]: None for n in UNSUPPORTED_NAMES})
_BY_LENGTH = sorted(NAMES, key=len, reverse=True)
# 핵심 단어가 같은데 나라가 다른 이름 (CHINA ↔ REPUBLIC OF CHINA). 이 이름만 보고는 정하지 않는다
_codes_by_core = {}
for _name, _code in NAMES.items():
    _codes_by_core.setdefault(_core(_name), set()).add(_code)
_AMBIGUOUS = {n for n in NAMES if n == _core(n) and len(_codes_by_core[n]) > 1}


def _exact(text):
    return (NAMES[text], True) if text in NAMES else None


def _word(text):
    # 긴 이름부터 본다. 'PEOPLES REPUBLIC OF CHINA'가 'REPUBLIC OF CHINA'·'CHINA'보다 먼저 걸리게
    for name in _BY_LENGTH:
        if re.search(rf"\b{name}\b", text):
            return (None, False) if name in _AMBIGUOUS else (NAMES[name], False)
    return None


def _fuzzy(text):
    core = _core(text)
    if len(core.replace(" ", "")) < MIN_LETTERS:
        return None
    best = {}  # 나라별 최고 점수 (지원 범위 밖은 None 키)
    for name, code in NAMES.items():
        ratio = SequenceMatcher(None, core, _core(name)).ratio()
        best[code] = max(best.get(code, 0.0), ratio)
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    (code, top), runner_up = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
    if top >= FUZZY_MIN and top - runner_up >= FUZZY_MARGIN:
        return code, False
    return None


def find_country(lines):
    """[(글자, OCR 신뢰도), ...] → Match, 못 찾으면 None.

    한 줄씩, 그다음 이웃한 두 줄을 이어 붙여(국가명이 두 줄로 읽힌 경우) 본다.
    정확 일치를 모든 후보에서 먼저 찾고, 없을 때만 단어 검색, 그다음 비슷한 이름으로 내려간다.
    """
    rows = [(normalize(t), s) for t, s in lines]
    rows = [(t, c, s) for (t, c), s in rows if len(t.replace(" ", "")) >= MIN_LETTERS]
    candidates = rows + [(f"{t1} {t2}", c1 or c2, min(s1, s2))
                         for (t1, c1, s1), (t2, c2, s2) in zip(rows, rows[1:])]
    for step in (_exact, _word, _fuzzy):
        for text, corrected, score in candidates:
            hit = step(text)
            if hit:
                code, sure = hit
                return Match(code, sure and not corrected, score)
    return None
