"""국적 변환 측정 목록 (11/24 확정). test_country.py와 scripts/eval_country.py가 같이 쓴다.

E-9 송출국 18개국(고용노동부 2026-08-14 보도자료 기준) × (흔한 이름, 공식 이름, 오타·두 줄 2개) = 양성 72개
+ 지원 범위 밖 음성 5개(null이 정답) = 77개.
'/'는 OCR이 두 줄로 나눠 읽은 경우. 공식 이름은 ISO·UN 명칭 기반이고 실제 카드 표기는 PHL만 확인했다.
"""

ROWS = {  # 코드: [흔한 이름, 공식 이름, 오타·두 줄 ①, 오타·두 줄 ②]
    "PHL": ["PHILIPPINES", "REPUBLIC OF THE PHILIPPINES", "오타:PHlLIPPINES", "두줄:REPUBLIC OF THE/PHILIPPINES"],
    "VNM": ["VIETNAM", "SOCIALIST REPUBLIC OF VIET NAM", "오타:VIETNM", "오타:V1ETNAM"],
    "IDN": ["INDONESIA", "REPUBLIC OF INDONESIA", "오타:lNDONESIA", "오타:INDONESA"],
    "THA": ["THAILAND", "KINGDOM OF THAILAND", "오타:THAlLAND", "오타:THAILND"],
    "KHM": ["CAMBODIA", "KINGDOM OF CAMBODIA", "오타:CAMB0DIA", "오타:CAMBODA"],
    "NPL": ["NEPAL", "FEDERAL DEMOCRATIC REPUBLIC OF NEPAL", "오타:NEPAI", "두줄:FEDERAL DEMOCRATIC REPUBLIC/OF NEPAL"],
    "MMR": ["MYANMAR", "REPUBLIC OF THE UNION OF MYANMAR", "오타:MYANMR", "오타:MYAMMAR"],
    "LKA": ["SRI LANKA", "DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA", "오타:SRILANKA", "오타:SRl LANKA"],
    "UZB": ["UZBEKISTAN", "REPUBLIC OF UZBEKISTAN", "오타:UZBEKlSTAN", "오타:UZBEKSTAN"],
    "BGD": ["BANGLADESH", "PEOPLE'S REPUBLIC OF BANGLADESH", "오타:BANGLADES", "오타:BANGIADESH"],
    "MNG": ["MONGOLIA", "MONGOLIA.", "오타:MONG0LIA", "오타:MONGOLA"],  # 따로 공식 이름이 없어 특수문자로 대체
    "PAK": ["PAKISTAN", "ISLAMIC REPUBLIC OF PAKISTAN", "오타:PAKlSTAN", "오타:PAKISTN"],
    "KGZ": ["KYRGYZSTAN", "KYRGYZ REPUBLIC", "오타:KYRGYSTAN", "오타:KYRGYZ5TAN"],
    "TLS": ["TIMOR-LESTE", "DEMOCRATIC REPUBLIC OF TIMOR-LESTE", "오타:TIMOR LESTE", "오타:TlMOR-LESTE"],
    "LAO": ["LAOS", "LAO PEOPLE'S DEMOCRATIC REPUBLIC", "오타:LA0S", "두줄:LAO PEOPLE'S DEMOCRATIC/REPUBLIC"],
    "CHN": ["CHINA", "PEOPLE'S REPUBLIC OF CHINA", "오타:CHlNA", "오타:CH1NA"],
    "TJK": ["TAJIKISTAN", "REPUBLIC OF TAJIKISTAN", "오타:TAJlKISTAN", "오타:TAJIKSTAN"],
    "KAZ": ["KAZAKHSTAN", "REPUBLIC OF KAZAKHSTAN", "오타:KAZAKH5TAN", "오타:KAZAKSTAN"],
}
NEGATIVE = ["REPUBLIC OF UTOPIA", "REPUBLIC OF CHINA", "INDIA", "REPUBLIC OF KOREA", "TAIWAN"]


def _cases():
    cases = []
    for code, (common, official, *typos) in ROWS.items():
        cases += [("흔한", common, code), ("공식", official, code)]
        cases += [tuple(t.split(":", 1)) + (code,) for t in typos]
    return cases + [("음성", text, None) for text in NEGATIVE]


CASES = _cases()  # [(유형, 입력, 기대 코드)]


def card_lines(text):
    """입력 글자를 등록증 국가 칸 모양의 OCR 줄 목록으로 만든다 (신뢰도 0.99 통일)"""
    values = [(t, 0.99) for t in text.split("/")]
    return [("국가/지역", 0.99)] + values + [("Country/Region", 0.99), ("체류자격", 0.99)]
