"""국적 변환 측정 (이미지·paddle 없이 글자만): 성공·null·잘못된 코드·사용자 재확인 수

실행: ocr 폴더에서  python scripts/eval_country.py
"""
import sys
from collections import Counter
from pathlib import Path

OCR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OCR))
sys.path.insert(0, str(OCR / "tests"))
from country_cases import CASES, card_lines  # noqa: E402
from pipeline.extract import extract_fields  # noqa: E402

REVIEW_THRESHOLD = 0.8  # Spring DocumentResponse.REVIEW_THRESHOLD와 같은 값
KINDS = ["흔한", "공식", "오타", "두줄", "음성"]

by, review = Counter(), Counter()
for kind, text, expected in CASES:
    f = {f["name"]: f for f in extract_fields(card_lines(text))}["NATIONALITY"]
    got, conf = f["value"], f["confidence"]
    result = "성공" if got == expected else ("null" if got is None else "잘못된코드")
    by[(kind, result)] += 1
    if got is not None and conf < REVIEW_THRESHOLD:
        review[result] += 1  # 값은 냈지만 사용자 확인 화면으로 넘어가는 칸
    if result != "성공" or (got is not None and conf < REVIEW_THRESHOLD):
        print(f"  [{result:5}] {kind} {text!r:45} 기대={expected} 결과={got} 신뢰도={conf}")

positive = [k for k in KINDS if k != "음성"]
n_pos = sum(1 for c in CASES if c[2] is not None)
ok = sum(by[(k, "성공")] for k in positive)
wrong = sum(by[(k, "잘못된코드")] for k in KINDS)
print(f"\n전체 {len(CASES)}개 (양성 {n_pos}, 음성 {len(CASES) - n_pos})")
print(f"양성 변환 성공 {ok}/{n_pos} ({ok / n_pos:.1%})")
print(f"잘못된 코드 {wrong} (0이어야 함)")
print(f"null (양성) {sum(by[(k, 'null')] for k in positive)}")
print(f"사용자 재확인 (값 있음, 신뢰도<{REVIEW_THRESHOLD}) {sum(review.values())} (그중 성공 {review['성공']})")
for k in KINDS:
    n = sum(by[(k, r)] for r in ["성공", "null", "잘못된코드"])
    print(f"  {k}: 성공 {by[(k, '성공')]}/{n}  null {by[(k, 'null')]}  잘못된코드 {by[(k, '잘못된코드')]}")
