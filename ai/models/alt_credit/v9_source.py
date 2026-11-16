# -*- coding: utf-8 -*-
"""
외국인 대안신용평가 — PD(부도확률) 기반 모델 V9 (가칭 '신한걸음 대안신용론' 상품 반영)
================================================
V9 = V8 + 상품 가입자격·조건 반영 + 외부 검토 반영
  · 가입자격: 국적·비자 제한 없음 / 근로 가능(취업 가능 체류자격 또는 시간제취업 허가) /
             체류만료 6개월 초과 / 최근 3개월 이상 연속 급여 입금 / 산출금리 20% 이하
  · 한도: 상품 최대 3천만원(씬파일러 위험 반영), 대출기간 = min(36, 남은 체류 개월 − 1) (만기 = 체류만료 전월)
  · 금리: 기타 가산 7.35% 고정(V8에서 SOL 공시 기준 역산한 값; 대상이 바뀌어도 다시 역산하지 않음)
          급여이체 우대 1.0%는 신한 급여이체자만, 거래실적 우대(급여+청약) 상한 1.5% 명시
  · 최소한도: 19.1%는 '이직률을 참고한 보수적 소득 감액 가정'(실제 소득 감소율 아님)
  · 월상환부담 40% 한도는 일반 DSR(연간)이 아닌 '간이 DSR'로 명칭 정리
  · 점수 범위 1~1,000점, 비자별 공정성 점검표·거절자 포함 금리 분포·유학생 예시·V8 대비표 추가
------------------------------------------------
(이하 V8 설명)
V8 = v7(신용점수·금리) + 대출한도 산정
  · 대출한도 (신한 SOL 글로벌론 한도 공식 기반)
      한도 = min(5천만원, 연소득×80%, 월소득×40%×대출개월수, DSR 상한) − 기존 대출
      DSR 상한: 월소득×40% − 기존대출 월상환액 으로, V8 산출금리·대출개월수·원금분할상환 기준 갚을 수 있는 원금
      최대한도: 실제 소득 · 산출금리      (신한 공식 그대로)
      최소한도: 소득×(1−이직률 19.1%) · 산출금리+1.5%p  (이직 위험 + 변동금리 상승 위험)
  · 이하 v7 설명
  · 대출금리 = 기준금리 + 가산금리 − 우대금리   (은행연합회 대출금리 모범규준 구조)
      기준금리: 신한은행 금융채(AAA) 6개월물, 변동금리(6개월마다 재산정)
      가산금리: 신용프리미엄(PD×LGD) + 자본비용(필요자본×자기자본비용) + 기타 가산(업무원가·유동성·목표이익)
               ※ 기타 가산은 '승인자 평균 가산금리 = SOL 글로벌론 공시 가산금리 9.12%'가 되도록 역산
               ※ 법적비용은 2026.7.1부터 가산금리 반영 금지 → 포함하지 않음
      우대금리: SOL 글로벌론 상품우대 (급여이체 1.0%, 청약 0.5%, 대환 0.3%, 웰컴 1.0%[한시, 기본 OFF])
  · 기본 정보(기존 통계 그대로): 비자, 누적 체류기간, 남은 체류기간, 월소득
  · 신용행동 변수(NICE 평가영역을 외국인용으로 대체)
      상환이력(28.4%)   → 최근 6개월 통신비 연체 횟수 (0~6)
      신용거래기간(12.3%) → 누적 체류기간
      비금융(7.3%)      → 최근 6개월 건강보험·공과금 연체 횟수 (0~6)
      부채수준(24.5%)   → 소득 대비 해외송금 비율 (0~0.8)
      신용형태(27.5%)   → 최근 6개월 중 해외송금한 달 수 (0~6, 규칙적일수록 위험↓)
                          송금을 안 하는 사람은 정보 없음 → 송금자 평균(중립값)으로 처리
  · 계수: 5개 영역의 영향 비중이 NICE 공시 비중(28.4 : 24.5 : 12.3 : 27.5 : 7.3)이 되도록 계산
  · 해외송금은 두 얼굴: 비율(부채수준)은 높을수록 위험↑, 규칙성(신용형태)은 높을수록 위험↓
  · 평균 부도율: 금감원 '신용대출 등(주담대 제외) 가계대출' 연체율 최신값 0.91% × 4.2
  · 소득·남은 체류기간은 PD가 아니라 한도·대출기간·거절 판단에 사용
"""
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, roc_curve

# =====================================================================
# 0. 가정·출처 파라미터
# =====================================================================
ASSUMPTIONS = {
    # --- 평균 부도율 ---
    "LOAN_DELINQ_LATEST": (0.0091, "[출처] 금감원 2026.8말 국내은행 '신용대출 등 주담대 제외 가계대출' 연체율 0.91%"),
    "FOREIGN_MULTIPLIER": (4.2, "[출처·추정] 전북은행 외국인대출 연체율 3%↑(뉴데일리 2024.11) ÷ 같은 시점 2024.6말 신용대출 등 가계대출 연체율 0.71%(금감원)"),
    # --- 해외송금 비율 (부채수준 대체) ---
    "REMIT_NONE_SHARE": (0.30, "[가정] 해외송금을 하지 않는 소득자 비율 → 이민자체류실태조사 송금 통계로 교체 예정"),
    "REMIT_MEAN": (0.30, "[가정] 송금자의 소득 대비 평균 송금 비율"),
    "REMIT_CONC": (6.0, "[가정] 송금 비율 분포의 집중도(베타분포, 클수록 평균 근처에 몰림)"),
    "REMIT_MAX": (0.80, "[가정] 송금 비율 상한"),
    "REMIT_REG_MEAN": (0.80, "[가정] 송금자가 한 달에 송금할 평균 확률 (최근 6개월 송금 개월 수 = 이항분포)"),
    "REMIT_REG_CONC": (4.0, "[가정] 송금 규칙성의 개인차 (베타분포 집중도)"),
    # --- 연체 횟수 분포 (0~6) ---
    "UTIL_LONG_SHARE": (0.043, "[출처] 외국인 건보 지역가입자 6개월 이상 체납 34,349세대 ÷ 79만9281명 (2025, 건보공단→국회)"),
    "UTIL_INTERMIT_SHARE": (0.10, "[가정] 건보·공과금 간헐 연체자 비율"),
    "TEL_LONG_SHARE": (0.043, "[가정] 통신비 6개월 연속 연체 비율 — 외국인 통계 없음, 건보와 동일 가정"),
    "TEL_INTERMIT_SHARE": (0.10, "[가정] 통신비 간헐 연체자 비율"),
    "INTERMIT_MONTHLY_P": (0.30, "[가정] 간헐 연체자가 한 달에 연체할 확률"),
    # --- 계수 비중 ---
    "W_REPAY": (28.4, "[출처] NICE 상환이력 활용비중 → 통신비 연체"),
    "W_DEBT": (24.5, "[출처] NICE 부채수준 활용비중 → 소득 대비 해외송금 비율"),
    "W_PATTERN": (27.5, "[출처] NICE 신용형태 활용비중 → 해외송금 규칙성"),
    "W_TENURE": (12.3, "[출처] NICE 신용거래기간 활용비중 → 누적 체류기간"),
    "W_NONFIN": (7.3, "[출처] NICE 비금융/마이데이터 활용비중 → 건보·공과금 연체"),
    "TARGET_AR": (0.57, "[출처] JB금융 Investor Day 2026 p.13: CB사 외국인 등급 AR 55.2~58.6% 중간값"),
    # --- 신용점수 ---
    "SCORE_SCALE_SOURCE": ("NICE 신용평점별 장기연체가능성", "[출처] NICE평가정보 신용등급체계공시(cb_score_3): 누적 구간별 '향후 1년 내 3개월 이상 연체 등록' 비율과 "
                           "구간별 인원(2025.12)으로 점수대별 불량률을 역산 → 점수 = offset + factor × ln(정상/불량) 을 최소제곱으로 적합. "
                           "500점 이하는 누적값 반올림·시점 차이로 역산값이 100%를 넘어 제외"),
    "SCORE_TOP_MID": (950, "[가정] 900~1000점 구간의 대표점수(중간값)"),
    # --- 금리 ---
    "BASE_RATE": (0.0383, "[출처] 신한은행 SOL 글로벌론 금리안내: 기준금리 금융채(6개월) 3.83% (2026.11.11). 변동금리·6개월 주기"),
    "BASE_RATE_DATE": ("2026-11-11", "[기준일] 기준금리·공시 가산금리 적용일 (같은 날짜 공시끼리 사용)"),
    "DISCLOSED_SPREAD": (0.0912, "[출처] 신한은행 SOL 글로벌론 금리안내: 가산금리 9.12% (2026.11.11, 2천만원·36개월·원금분할·건별 가정)"),
    "DISCLOSED_RATE_MIN": (0.1014, "[출처] SOL 글로벌론 공시 최저금리 10.14% (우대 2.8% 최대 적용, 웰컴우대 포함)"),
    "DISCLOSED_RATE_MAX": (0.1295, "[출처] SOL 글로벌론 공시 최고금리 12.95% (우대 미적용)"),
    "LGD": (0.45, "[출처] 바젤 기초IRB 선순위 무담보 LGD 45% (내국인 기준). 한계: 외국인 건보료 결손처분의 95.9%가 장기출국(건보공단 2023~25) → 외국인 실제 LGD는 더 높을 수 있음"),
    "HURDLE_RATE": (0.10, "[가정] 자기자본비용(목표 ROE) → 신한금융 밸류업 공시 목표 ROE로 교체 예정"),
    "OTHER_SPREAD": (0.073547, "[역산·고정] 기타 가산(업무원가·유동성프리미엄·목표이익률) = V8에서 'SOL 승인자 평균 가산금리 = 공시 9.12%'로 역산한 7.35%. "
                               "V9은 SOL보다 위험한 씬파일러를 받으므로 다시 역산하지 않고 고정 → 평균 가산금리가 위험만큼 9.12%보다 높게 나옴"),
    # --- 우대금리 (SOL 글로벌론 상품안내와 동일) ---
    "PREF_SALARY": (0.010, "[출처] SOL 글로벌론 거래실적 우대: 급여이체 연 1.0% → V9: 신한 급여이체자만 적용 (가입 요건이 아니라 우대 조건)"),
    "PREF_TXN_MAX": (0.015, "[출처] SOL 글로벌론 거래실적 우대금리 최고 연 1.5% (급여이체+청약)"),
    "SHINHAN_PAYROLL_SHARE": (0.50, "[가정] 대출 대상 중 신한은행 급여이체자 비율 (타행·마이데이터 급여 확인자도 가입 가능)"),
    "PREF_SUBSCRIPTION": (0.005, "[출처] SOL 글로벌론 거래실적 우대: 청약 저축 유지 시 연 0.5%"),
    "PREF_REFINANCE": (0.003, "[출처] SOL 글로벌론 다른 금융기관 대출 대환 시 연 0.3% (대면 채널)"),
    "PREF_WELCOME": (0.010, "[출처] SOL 글로벌론 웰컴우대 연 1.0% ('26.12.31까지 한시)"),
    "USE_WELCOME": (False, "[설정] 웰컴우대는 한시 이벤트라 기본 모델에서 제외 (True로 바꾸면 적용)"),
    "PREF_MAX": (0.028, "[출처] SOL 글로벌론 상품우대금리 최대 2.8%"),
    "SUBSCRIPTION_SHARE": (0.10, "[가정] 청약저축 보유 비율"),
    "REFINANCE_SHARE": (0.05, "[가정] 타 금융기관 대출 대환 신청 비율"),
    "MAX_LEGAL_RATE": (0.20, "[출처] 법정 최고금리 연 20%"),
    # --- 한도 ---
    "TURNOVER_RATE": (0.191, "[가정] 최소한도용 보수적 소득 감액률. 이민자체류실태조사 1년 이내 이직률 19.1%를 참고한 값이며 실제 소득 감소율은 아님"),
    "PAYMENT_RATIO": (0.40, "[출처] SOL 글로벌론 한도: 월소득×40%×대출개월수, 차주별 DSR 40% → 모델은 첫 달 상환액 기준 '간이 DSR'(일반 DSR은 연간 원리금/연소득)"),
    "PRODUCT_MAX": (30_000_000, "[상품] 신한걸음 대안신용론(가칭) 최대 한도 3천만원 (SOL 글로벌론 5천만원보다 낮춤: 씬파일러 정보 부족·위험 반영)"),
    "INCOME_CAP_RATIO": (0.80, "[출처] SOL 글로벌론 한도: 연소득×80% (6·27 대책 '연소득 이내'보다 엄격)"),
    "EXISTING_DEBT_DEFAULT": (0, "[가정] 데모에서는 기존 대출 0원. 실제 운영 시 신용정보원 조회로 잔액·월상환액 차감"),
    "LIMIT_UNIT": (100_000, "[설정] 한도 10만원 단위 절사"),
    "MAX_TERM": (36, "[출처] SOL 글로벌론 최장 36개월 (동일)"),
    "MIN_REMAINING": (6, "[상품] 체류 만료 6개월 초과 요건 (SOL 글로벌론과 동일)"),
    "TERM_BEFORE_EXPIRY": (1, "[상품] 대출 만기일은 체류기간 만료일 전월까지 → 대출기간 = min(36, 남은 체류 개월 − 1)"),
    "MIN_SALARY_MONTHS": (3, "[상품] 최근 3개월 이상 연속 급여 입금 (신한 급여이체 또는 마이데이터 타행 급여)"),
    # --- V9 목데이터 가정 (작동 확인용) ---
    "STUDENT_INCOME_RANGE": ((50, 150), "[가정] 유학생 소득은 시간제 아르바이트 수준 월 50~150만원 균등분포"),
    "STUDENT_PERMIT_SHARE": (0.85, "[가정] 소득이 있는 유학생 중 시간제취업 허가 보유 비율 (나머지는 근로 불가로 대상 아님)"),
    "OTHER_PERMIT_SHARE": (0.90, "[가정] '기타' 체류자격 소득자 중 취업 가능 자격 보유 비율"),
    "SHORT_SALARY_SHARE": (0.10, "[가정] 급여 입금 연속 개월 수가 3개월 미만(1~2개월)인 소득자 비율"),
    "STUDENT_SHORT_SALARY_SHARE": (0.20, "[가정] 유학생의 급여 입금 3개월 미만 비율 (시간제라 불규칙)"),
    "STRESS_RATE_ADD": (0.015, "[가정] 최소한도용 금리 상승 가산 (변동금리 6개월 재산정 위험, 스트레스 DSR 개념 차용)"),
    "RENEWAL_CYCLE": (36, "[가정] 갱신형 비자(E-7·F-4·F-6) 허가 주기"),
    # --- 벤치마크 ---
    "BENCH_AVG_LOAN": (11_630_000, "[출처] JB 외국인대출 1조473억 ÷ 9만명 (Investor Day 2026 p.6)"),
    "BENCH_RATE_MIN": (0.1017, "[출처] JB 브라보코리아 금리 하단"),
    "BENCH_RATE_MAX": (0.1790, "[출처] JB 브라보코리아 금리 상단"),
}
A = {k: v[0] for k, v in ASSUMPTIONS.items()}
A["TARGET_BAD_RATE"] = A["LOAN_DELINQ_LATEST"] * A["FOREIGN_MULTIPLIER"]

# =====================================================================
# 1. 기존 통계표 (11월 4일 버전 그대로)
# =====================================================================
visa_categories = ["재외동포(F-4)", "비전문취업(E-9)", "유학생(D-2/D-4)", "영주(F-5)",
                   "결혼이민(F-6)", "전문인력(E-1~E-7)", "방문취업(H-2)", "기타"]
visa_probs = [0.242, 0.190, 0.140, 0.096, 0.076, 0.049, 0.043, 0.164]
employment_rates = {"비전문취업(E-9)": 0.999, "전문인력(E-1~E-7)": 0.994, "영주(F-5)": 0.757,
                    "방문취업(H-2)": 0.741, "결혼이민(F-6)": 0.605, "재외동포(F-4)": 0.618,
                    "유학생(D-2/D-4)": 0.235, "기타": 0.513}
stay_duration_ranges = {"비전문취업(E-9)": (6, 58), "방문취업(H-2)": (6, 58), "전문인력(E-1~E-7)": (6, 120),
                        "유학생(D-2/D-4)": (6, 48), "재외동포(F-4)": (6, 120), "영주(F-5)": (12, 120),
                        "결혼이민(F-6)": (6, 120), "기타": (6, 48)}
wage_distribution = {"비전문취업(E-9)": [0.000, 0.005, 0.689, 0.306], "전문인력(E-1~E-7)": [0.000, 0.016, 0.477, 0.508],
                     "유학생(D-2/D-4)": [0.362, 0.512, 0.126, 0.000], "방문취업(H-2)": [0.030, 0.117, 0.428, 0.424],
                     "재외동포(F-4)": [0.027, 0.093, 0.417, 0.463], "영주(F-5)": [0.023, 0.071, 0.313, 0.593],
                     "결혼이민(F-6)": [0.063, 0.176, 0.489, 0.271], "기타": [0.043, 0.126, 0.558, 0.272]}
FIXED_TERM = {"비전문취업(E-9)": 58, "방문취업(H-2)": 58, "유학생(D-2/D-4)": 48, "기타": 48}
NO_EXPIRY = {"영주(F-5)"}
STUDENT = "유학생(D-2/D-4)"
PERMIT_SHARE = {STUDENT: A["STUDENT_PERMIT_SHARE"], "기타": A["OTHER_PERMIT_SHARE"]}   # 나머지 체류자격은 취업 가능


def remaining_to_expiry(visa, stay):
    if visa in NO_EXPIRY:
        return np.nan
    if visa in FIXED_TERM:
        return max(0, FIXED_TERM[visa] - stay)
    cycle = A["RENEWAL_CYCLE"]
    return cycle - (stay % cycle)


# =====================================================================
# 2. 목데이터 생성
# =====================================================================
def delinquency_counts(rng, n, long_share, intermit_share, p_month):
    """최근 6개월 연체 횟수(0~6): 성실(0회) / 간헐(이항분포, 1회 이상) / 장기(6회) 3개 집단 혼합."""
    group = rng.choice(3, size=n, p=[1 - long_share - intermit_share, intermit_share, long_share])
    counts = np.zeros(n, dtype=int)
    idx = np.where(group == 1)[0]
    c = rng.binomial(6, p_month, size=len(idx))
    while (c == 0).any():                                  # 간헐 집단은 최소 1회
        z = c == 0
        c[z] = rng.binomial(6, p_month, size=z.sum())
    counts[idx] = c
    counts[group == 2] = 6
    return counts


def generate_population(n, seed):
    rng = np.random.default_rng(seed)
    visas = rng.choice(visa_categories, size=n, p=visa_probs)
    stay, inc, wb = [], [], []
    for v in visas:
        lo, hi = stay_duration_ranges[v]
        stay.append(int(rng.integers(lo, hi + 1)))
        if rng.random() >= employment_rates[v]:
            inc.append(0); wb.append(0); continue
        if v == STUDENT:                                   # V9: 유학생은 시간제 아르바이트 수준 소득
            lo_i, hi_i = A["STUDENT_INCOME_RANGE"]
            m = int(rng.integers(lo_i, hi_i + 1)); wb.append(1 if m < 100 else 2)
            inc.append(m * 10000); continue
        probs = np.array(wage_distribution[v]); probs = probs / probs.sum()
        b = int(rng.choice([1, 2, 3, 4], p=probs)); wb.append(b)
        lo_i, hi_i = {1: (50, 100), 2: (100, 200), 3: (200, 300), 4: (300, 550)}[b]
        inc.append(int(rng.integers(lo_i, hi_i)) * 10000)
    df = pd.DataFrame({"user_id": [f"user_{i:04d}" for i in range(1, n + 1)], "visa_type": visas,
                       "stay_duration_months": stay, "wage_bracket": wb, "monthly_income": inc})
    df["months_to_expiry"] = [remaining_to_expiry(v, s) for v, s in zip(df.visa_type, df.stay_duration_months)]
    df["tel_late_6m"] = delinquency_counts(rng, n, A["TEL_LONG_SHARE"], A["TEL_INTERMIT_SHARE"], A["INTERMIT_MONTHLY_P"])
    df["util_late_6m"] = delinquency_counts(rng, n, A["UTIL_LONG_SHARE"], A["UTIL_INTERMIT_SHARE"], A["INTERMIT_MONTHLY_P"])
    # 소득 대비 해외송금 비율: 비송금자 0, 송금자는 베타분포 (소득 없는 사람은 0)
    a = A["REMIT_MEAN"] * A["REMIT_CONC"]; b = (1 - A["REMIT_MEAN"]) * A["REMIT_CONC"]
    remit = np.where(rng.random(n) < A["REMIT_NONE_SHARE"], 0.0,
                     np.minimum(rng.beta(a, b, size=n), A["REMIT_MAX"]))
    df["remit_ratio"] = np.where(df["monthly_income"] > 0, remit.round(3), 0.0)
    # 해외송금 규칙성: 송금자는 최근 6개월 중 송금한 달 수(1~6), 비송금자는 NaN → 아래에서 중립값으로 대체
    ar = A["REMIT_REG_MEAN"] * A["REMIT_REG_CONC"]; br = (1 - A["REMIT_REG_MEAN"]) * A["REMIT_REG_CONC"]
    months = np.maximum(rng.binomial(6, rng.beta(ar, br, size=n)), 1)
    df["remit_months_6m"] = np.where(df["remit_ratio"] > 0, months, np.nan)
    neutral = df.loc[df["remit_ratio"] > 0, "remit_months_6m"].mean()
    df["remit_regularity"] = df["remit_months_6m"].fillna(neutral).round(3)
    # SOL 글로벌론 우대금리 대상 여부 (소득자만 해당)
    earner = df["monthly_income"] > 0
    df["has_subscription"] = ((rng.random(n) < A["SUBSCRIPTION_SHARE"]) & earner).astype(int)
    df["is_refinance"] = ((rng.random(n) < A["REFINANCE_SHARE"]) & earner).astype(int)
    # V9 가입자격 변수 (소득자만 의미 있음)
    permit_p = df["visa_type"].map(PERMIT_SHARE).fillna(1.0).values
    df["work_permitted"] = ((rng.random(n) < permit_p) & earner).astype(int)          # 취업 가능 자격 또는 시간제취업 허가
    short_p = np.where(df["visa_type"] == STUDENT, A["STUDENT_SHORT_SALARY_SHARE"], A["SHORT_SALARY_SHARE"])
    sal_m = np.where(rng.random(n) < short_p, rng.integers(1, 3, size=n), rng.integers(3, 13, size=n))
    df["salary_months"] = np.where(earner, sal_m, 0)                                  # 최근 연속 급여 입금 개월 수 (최대 12)
    df["shinhan_payroll"] = ((rng.random(n) < A["SHINHAN_PAYROLL_SHARE"]) & earner).astype(int)
    df["effective_monthly_income"] = df["monthly_income"] * (1 - A["TURNOVER_RATE"])
    df["existing_debt"] = A["EXISTING_DEBT_DEFAULT"]               # 기존 대출 잔액 (데모: 0)
    df["existing_monthly_payment"] = 0                              # 기존 대출 월상환액 (데모: 0)
    return df


# =====================================================================
# 3. 계수: NICE 비중 → 변수별 계수 / AR·부도율로 크기 계산
# =====================================================================
FEATURES = ["tel_late_6m", "remit_ratio", "stay_duration_months", "remit_regularity", "util_late_6m"]
SIGNS = np.array([+1, +1, -1, -1, +1])                # 연체·송금비율↑ 위험↑ / 체류·송금규칙성↑ 위험↓
WEIGHTS = np.array([A["W_REPAY"], A["W_DEBT"], A["W_TENURE"], A["W_PATTERN"], A["W_NONFIN"]])
WEIGHTS = WEIGHTS / WEIGHTS.sum()                     # NICE 5개 영역 합계 100%


def base_betas(pop):
    """|β_i| × 표준편차_i 가 NICE 비중에 비례하도록 계수 방향·상대크기를 정함."""
    sd = pop[FEATURES].std().values
    return SIGNS * WEIGHTS / sd, pop[FEATURES].mean().values


def latent(pop, betas, means, scale):
    return scale * ((pop[FEATURES].values - means) @ betas)


def calibrate_intercept(lin, target):
    return brentq(lambda a: (1 / (1 + np.exp(-(a + lin)))).mean() - target, -25, 10)


def ks_stat(y, p):
    fpr, tpr, _ = roc_curve(y, p)
    return float(np.max(tpr - fpr))


def fit(pop, betas, means, scale, target_bad, seed=11):
    pop = pop.copy()
    lin = latent(pop, betas, means, scale)
    alpha = calibrate_intercept(lin, target_bad)
    rng = np.random.default_rng(seed)
    pop["true_pd"] = 1 / (1 + np.exp(-(alpha + lin)))
    pop["default"] = (rng.random(len(pop)) < pop["true_pd"]).astype(int)
    split = int(len(pop) * 0.7)
    tr, te = pop.iloc[:split], pop.iloc[split:]
    model = LogisticRegression(max_iter=2000).fit(tr[FEATURES], tr["default"])
    p = model.predict_proba(te[FEATURES])[:, 1]
    auc = roc_auc_score(te["default"], p)
    return model, dict(alpha=alpha, auc=auc, ar=2 * auc - 1, ks=ks_stat(te["default"], p),
                       n_train=len(tr), n_test=len(te), test_bad=te["default"].mean(), pred_pd=p.mean())


def solve_scale(pop, betas, means, target_bad, target_ar):
    lo, hi = 0.1, 20.0
    for _ in range(22):
        mid = (lo + hi) / 2
        if fit(pop, betas, means, mid, target_bad)[1]["ar"] < target_ar:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# =====================================================================
# 4. 점수 · 금리 · 한도 (v2와 동일)
# =====================================================================
# NICE 공시: 신용평점별 인원수(2025.12)와 누적 장기연체가능성
NICE_COUNT = {900: 23804368, 800: 10712715, 700: 12275779, 600: 613240,
              500: 122167, 400: 47922, 300: 1936927, 200: 171734, 100: 2378}
NICE_CUM_BAD = {900: .0011, 800: .0034, 700: .0076, 600: .0098, 500: .0114,
                400: .0126, 300: .0127, 200: .0127, 100: .0127}


def nice_band_bad_rates():
    """누적 불량률 → 점수대별 불량률로 역산."""
    rows, n_cum, prev_bad = [], 0, 0.0
    for k in sorted(NICE_COUNT, reverse=True):
        n_cum += NICE_COUNT[k]
        bad_cum = NICE_CUM_BAD[k] * n_cum
        band_bad = bad_cum - prev_bad
        prev_bad = bad_cum
        rows.append({"점수대": f"{k}~{k + 99 if k < 900 else 1000}", "하한": k, "인원": NICE_COUNT[k],
                     "역산 불량률": band_bad / NICE_COUNT[k],
                     "반올림 오차(±)": 0.00005 * n_cum / NICE_COUNT[k]})
    out = pd.DataFrame(rows)
    out["보정에 사용"] = out["하한"] >= 600
    return out


def fit_score_scale():
    band = nice_band_bad_rates()
    use = band[band["보정에 사용"]].copy()
    use["대표점수"] = np.where(use["하한"] == 900, A["SCORE_TOP_MID"], use["하한"] + 50)
    x = np.log((1 - use["역산 불량률"]) / use["역산 불량률"]).values
    X = np.vstack([np.ones(len(x)), x]).T
    off, fac = np.linalg.lstsq(X, use["대표점수"].values, rcond=None)[0]
    band["대표점수"] = np.where(band["하한"] == 900, A["SCORE_TOP_MID"], band["하한"] + 50)
    band["우리 척도 적합점수"] = np.where(band["보정에 사용"],
                                   (off + fac * np.log((1 - band["역산 불량률"].clip(1e-4, .99)) / band["역산 불량률"].clip(1e-4, .99))).round(),
                                   np.nan)
    return off, fac, band


offset, factor, NICE_BAND = fit_score_scale()


def pd_to_score(p):
    return np.clip(offset + factor * np.log((1 - p) / p), 1, 1000).round()      # V9: 1~1,000점


def score_band(s):
    if s > 950: return "951~1000"
    if s <= 600: return "600 이하"
    lo = int((s - 1) // 50) * 50 + 1
    return f"{lo}~{lo + 49}"


def basel_capital(p, lgd):
    p = np.clip(p, 1e-5, 0.999)
    w = (1 - np.exp(-35 * p)) / (1 - np.exp(-35))
    r = 0.03 * w + 0.16 * (1 - w)
    return np.maximum(lgd * norm.cdf((norm.ppf(p) + np.sqrt(r) * norm.ppf(0.999)) / np.sqrt(1 - r)) - p * lgd, 0)


def loan_rate(p, has_sub, is_refi, payroll):
    """대출금리 = 기준금리 + 가산금리 − 우대금리 (구성요소별로 반환)."""
    credit_premium = p * A["LGD"]                                    # 신용프리미엄(기대손실)
    capital_cost = basel_capital(p, A["LGD"]) * A["HURDLE_RATE"]     # 자본비용
    spread = credit_premium + capital_cost + A["OTHER_SPREAD"]       # 가산금리 (기타 가산 고정)
    txn = np.minimum(A["PREF_SALARY"] * payroll + A["PREF_SUBSCRIPTION"] * has_sub, A["PREF_TXN_MAX"])  # 거래실적 우대(최고 1.5%)
    pref = txn + A["PREF_REFINANCE"] * is_refi + (A["PREF_WELCOME"] if A["USE_WELCOME"] else 0.0)
    pref = np.minimum(pref, A["PREF_MAX"])                           # 상품우대 상한 2.8%
    final = A["BASE_RATE"] + spread - pref
    return dict(rate_base=np.full_like(p, A["BASE_RATE"]), rate_credit_premium=credit_premium,
                rate_capital_cost=capital_cost, rate_other_spread=np.full_like(p, A["OTHER_SPREAD"]),
                rate_spread=spread, rate_preferential=pref, rate=final)


# V9: 기타 가산 역산(calibrate_other_spread)은 삭제. V8에서 SOL 기준으로 구한 7.35%를 ASSUMPTIONS에 고정값으로 사용.


def max_principal(pay, rate, n):
    r = rate / 12
    return pay * (1 - (1 + r) ** -n) / r if n > 0 else 0.0


def dsr_max_principal(monthly_income, existing_payment, annual_rate, n):
    """간이 DSR(월상환부담 40%) 상한: 원금분할상환 첫 달 상환액(원금 P/n + 이자 P×r)이 월소득×40% − 기존 상환액 이하.
    ※ 일반 DSR(연간 원리금 ÷ 연소득)과 다른 간이 산식."""
    room = monthly_income * A["PAYMENT_RATIO"] - existing_payment
    if room <= 0 or n <= 0:
        return 0.0
    return room / (1 / n + annual_rate / 12)


def limit_components(monthly_income, existing_debt, existing_payment, rate, n):
    """신한 SOL 글로벌론 한도 공식의 4개 상한과 최종 한도."""
    comp = {
        "상품 최대(3천만)": A["PRODUCT_MAX"],
        "연소득×80%": monthly_income * 12 * A["INCOME_CAP_RATIO"],
        "월소득×40%×개월수": monthly_income * A["PAYMENT_RATIO"] * n,
        "간이 DSR 40%": dsr_max_principal(monthly_income, existing_payment, rate, n),
    }
    binding = min(comp, key=comp.get)
    limit = max(0.0, comp[binding] - existing_debt)
    return int(limit // A["LIMIT_UNIT"] * A["LIMIT_UNIT"]), binding, comp


def decide(row):
    out = dict(loan_term=0, min_limit=0, max_limit=0, max_limit_binding="", min_limit_binding="",
               cap_product=np.nan, cap_income80=np.nan, cap_payment=np.nan, cap_dsr=np.nan,
               decision="승인", reason="")
    if row["effective_monthly_income"] <= 0:
        out.update(decision="대상아님", reason="소득 없음"); return pd.Series(out)
    if row["work_permitted"] != 1:
        out.update(decision="대상아님", reason="근로 가능 자격 미확인(시간제취업 허가 없음 등)"); return pd.Series(out)
    if row["salary_months"] < A["MIN_SALARY_MONTHS"]:
        out.update(decision="대상아님", reason=f"급여 입금 {A['MIN_SALARY_MONTHS']}개월 미만"); return pd.Series(out)
    mte = row["months_to_expiry"]
    if not np.isnan(mte) and mte <= A["MIN_REMAINING"]:
        out.update(decision="거절", reason=f"체류만료 {A['MIN_REMAINING']}개월 이내"); return pd.Series(out)
    if row["rate"] > A["MAX_LEGAL_RATE"]:
        out.update(decision="거절", reason="산출금리가 법정최고금리 초과"); return pd.Series(out)
    # 만기 = 체류만료 전월 → 대출기간 = min(36, 남은 개월 − 1)
    term = int(min(A["MAX_TERM"], (mte - A["TERM_BEFORE_EXPIRY"]) if not np.isnan(mte) else A["MAX_TERM"]))
    # 최대한도: 실제 소득 · 산출금리 (신한 공식 그대로)
    hi, hi_bind, comp = limit_components(row["monthly_income"], row["existing_debt"], row["existing_monthly_payment"],
                                         row["rate"], term)
    # 최소한도: 이직률 감가 소득 · 금리 상승 가산
    lo, lo_bind, _ = limit_components(row["effective_monthly_income"], row["existing_debt"], row["existing_monthly_payment"],
                                      row["rate"] + A["STRESS_RATE_ADD"], term)
    out.update(loan_term=term, max_limit=hi, min_limit=min(lo, hi), max_limit_binding=hi_bind, min_limit_binding=lo_bind,
               cap_product=comp["상품 최대(3천만)"], cap_income80=comp["연소득×80%"],
               cap_payment=comp["월소득×40%×개월수"], cap_dsr=comp["간이 DSR 40%"])
    return pd.Series(out)


# =====================================================================
# 5. 실행
# =====================================================================
V8_RESULT = {  # V8 최종 결과 (비교용, V8 실행 결과에서 옮김)
    "승인": 565, "대상아님": 345, "거절": 90, "평균 점수(전체)": 783.6, "승인자 평균 PD": 0.0293,
    "승인자 평균 가산금리": 0.0912, "승인자 평균 우대금리": 0.0106, "승인자 평균 금리": 0.1189,
    "승인자 최저 금리": 0.0981, "승인자 최고 금리": 0.1989, "승인자 평균 최대한도": 22_906_194,
    "승인자 평균 최소한도": 18_320_530, "기타 가산": 0.073547, "AR": 0.5702,
}


def student_grid(monthly_income=1_000_000, scores=(850, 800, 750, 700, 650, 610), terms=(6, 11, 23, 35),
                 payroll=1):
    """유학생 예시: 월소득 고정, 점수·대출기간별 금리와 최소~최대 한도."""
    rows = []
    for s in scores:
        p = 1 / (1 + np.exp((s - offset) / factor))
        r = float(loan_rate(np.array([p]), np.array([0]), np.array([0]), np.array([payroll]))["rate"][0])
        row = {"점수": s, "PD": round(p, 4), "금리": round(r, 4), "판정": "승인" if r <= A["MAX_LEGAL_RATE"] else "거절(20% 초과)"}
        for n in terms:
            hi = limit_components(monthly_income, 0, 0, r, n)[0]
            lo = limit_components(monthly_income * (1 - A["TURNOVER_RATE"]), 0, 0, r + A["STRESS_RATE_ADD"], n)[0]
            row[f"대출기간 {n}개월 (최소~최대, 만원)"] = (f"{min(lo, hi) // 10000:,}~{hi // 10000:,}"
                                                     if r <= A["MAX_LEGAL_RATE"] else "-")
        rows.append(row)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    # 학습용 모집단 (소득자만 = 대출 대상)
    train = generate_population(60000, seed=7)
    train = train[train.monthly_income > 0].reset_index(drop=True)
    betas, means = base_betas(train)
    scale = solve_scale(train, betas, means, A["TARGET_BAD_RATE"], A["TARGET_AR"])
    model, m = fit(train, betas, means, scale, A["TARGET_BAD_RATE"])

    # 검증 1) 영향 비중이 NICE 비율대로 나오는지
    sd = train[FEATURES].std().values
    contrib = np.abs(model.coef_[0]) * sd
    share_df = pd.DataFrame({"변수": FEATURES, "대응 NICE 영역": ["상환이력", "부채수준", "신용거래기간", "신용형태", "비금융"],
                             "목표 비중": WEIGHTS.round(3), "추정 모델 비중": (contrib / contrib.sum()).round(3),
                             "생성 계수": (betas * scale).round(4), "추정 계수": model.coef_[0].round(4)})

    # 데모 1,000명
    df = generate_population(1000, seed=42)
    inc = df.monthly_income > 0
    df["pd"] = model.predict_proba(df[FEATURES])[:, 1]
    df["credit_score"] = pd_to_score(df["pd"])
    df["score_band"] = [score_band(s) for s in df["credit_score"]]

    # 금리는 소득자에게만 산출 (기타 가산 7.35% 고정, 급여이체 우대는 신한 급여이체자만)
    parts = loan_rate(df["pd"].values, df["has_subscription"].values, df["is_refinance"].values, df["shinhan_payroll"].values)
    for k, v in parts.items():
        df[k] = np.where(inc, v, np.nan)
    df = pd.concat([df, df.apply(decide, axis=1)], axis=1)
    ok = df[df.decision == "승인"]

    # 점수표
    neutral = df.loc[df.remit_ratio > 0, "remit_months_6m"].mean()
    ref = {f: 0.0 for f in FEATURES}; ref["remit_regularity"] = neutral
    b0, bs = model.intercept_[0], model.coef_[0]
    base_pts = offset - factor * (b0 + sum(b * ref[f] for f, b in zip(FEATURES, bs)))
    item_pts = {f: -factor * b for f, b in zip(FEATURES, bs)}
    card_rows = [("기본점수", "-", "", round(base_pts, 1))]
    labels = {"tel_late_6m": ("상환이력", "통신비 연체 1회당"), "remit_ratio": ("부채수준", "해외송금 비율 10%p당"),
              "stay_duration_months": ("신용거래기간", "누적 체류 12개월당"), "remit_regularity": ("신용형태", "송금 개월 수가 평균보다 1개월 많을 때마다"),
              "util_late_6m": ("비금융", "건보·공과금 연체 1회당")}
    unit = {"tel_late_6m": 1, "remit_ratio": 0.1, "stay_duration_months": 12, "remit_regularity": 1, "util_late_6m": 1}
    for f in FEATURES:
        card_rows.append((labels[f][0], labels[f][1], f, round(item_pts[f] * unit[f], 1)))
    card_rows.append(("(참고)", f"송금 규칙성 평균(중립값) = {neutral:.2f}개월, 송금 안 하는 사람은 0점", "", None))
    scorecard = pd.DataFrame(card_rows, columns=["NICE 영역", "항목", "변수", "점수"])
    recon = base_pts + sum(item_pts[f] * (df[f] - ref[f]) for f in FEATURES)
    max_gap = float(np.abs(np.clip(recon, 1, 1000).round() - df["credit_score"]).max())

    # 검증 2) 변수별 단조성
    mono = (df[inc].groupby("tel_late_6m").agg(인원=("user_id", "size"), 평균PD=("pd", "mean"),
                                               평균점수=("credit_score", "mean")).round(4))
    mono_u = (df[inc].groupby("util_late_6m").agg(인원=("user_id", "size"), 평균PD=("pd", "mean"),
                                                 평균점수=("credit_score", "mean")).round(4))
    bins = pd.cut(df.loc[inc, "remit_ratio"], [-0.001, 0, 0.2, 0.4, 0.6, 0.8], labels=["0 (송금 안 함)", "0~20%", "20~40%", "40~60%", "60~80%"])
    mono_r = (df[inc].groupby(bins, observed=False).agg(인원=("user_id", "size"), 평균PD=("pd", "mean"),
                                                      평균점수=("credit_score", "mean")).round(4))
    mono_r.index.name = "해외송금 비율"
    reg = df.loc[inc, "remit_months_6m"].fillna(-1).astype(int).replace(-1, "송금 안 함(중립)")
    mono_g = (df[inc].groupby(reg).agg(인원=("user_id", "size"), 평균PD=("pd", "mean"),
                                       평균점수=("credit_score", "mean")).round(4))
    mono_g.index.name = "최근 6개월 송금 개월 수"

    metrics = pd.DataFrame([
        ("목표 부도율 (0.91% × 4.2)", round(A["TARGET_BAD_RATE"], 4), "학습 모집단(소득자)"),
        ("검증 부도율", round(m["test_bad"], 4), "검증 표본"), ("평균 예측 PD", round(m["pred_pd"], 4), "검증 표본"),
        ("AUC", round(m["auc"], 4), "검증 표본"), ("AR(Gini)", round(m["ar"], 4), "검증 표본"), ("KS", round(m["ks"], 4), "검증 표본"),
        ("계수 공통배율(계산)", round(scale, 4), ""), ("절편(계산)", round(m["alpha"], 4), ""),
        ("학습/검증 표본", f'{m["n_train"]}/{m["n_test"]}', ""),
        ("점수 척도 offset(NICE 보정)", round(offset, 1), ""), ("점수 척도 factor(NICE 보정)", round(factor, 1), ""),
        ("PDO(=factor×ln2)", round(factor * np.log(2), 1), ""), ("점수표 재계산 최대 오차(점)", max_gap, "데모 1,000명"),
        ("기타 가산(고정)", A["OTHER_SPREAD"], "V8 SOL 기준 역산값"),
        ("승인자 평균 가산금리", round(ok.rate_spread.mean(), 4), "승인자"), ("SOL 공시 가산금리(참고)", A["DISCLOSED_SPREAD"], "SOL 글로벌론"),
    ], columns=["지표", "값", "기준"])

    # 판정 요약
    decision_tab = df.groupby(["decision", "reason"]).size().rename("인원").reset_index()
    decision_tab["reason"] = decision_tab["reason"].replace("", "-")

    # 비교표 (각 수치의 기준 명시)
    bench = pd.DataFrame([
        ("승인 인원", "전체 1,000명 중", len(ok), V8_RESULT["승인"]),
        ("대상아님 인원", "전체 1,000명 중", int((df.decision == "대상아님").sum()), V8_RESULT["대상아님"]),
        ("거절 인원", "전체 1,000명 중", int((df.decision == "거절").sum()), V8_RESULT["거절"]),
        ("평균 점수", "전체 1,000명", round(df.credit_score.mean(), 1), V8_RESULT["평균 점수(전체)"]),
        ("평균 PD", "승인자", round(ok.pd.mean(), 4), V8_RESULT["승인자 평균 PD"]),
        ("평균 가산금리", "승인자", round(ok.rate_spread.mean(), 4), V8_RESULT["승인자 평균 가산금리"]),
        ("평균 우대금리", "승인자", round(ok.rate_preferential.mean(), 4), V8_RESULT["승인자 평균 우대금리"]),
        ("평균 금리", "승인자", round(ok.rate.mean(), 4), V8_RESULT["승인자 평균 금리"]),
        ("최저 금리", "승인자", round(ok.rate.min(), 4), V8_RESULT["승인자 최저 금리"]),
        ("최고 금리", "승인자", round(ok.rate.max(), 4), V8_RESULT["승인자 최고 금리"]),
        ("평균 최대한도(원)", "승인자", int(ok.max_limit.mean()), V8_RESULT["승인자 평균 최대한도"]),
        ("평균 최소한도(원)", "승인자", int(ok.min_limit.mean()), V8_RESULT["승인자 평균 최소한도"]),
        ("최대한도 범위(원)", "승인자", f"{int(ok.max_limit.min()):,} ~ {int(ok.max_limit.max()):,}", "2,600,000 ~ 50,000,000"),
        ("기타 가산", "모델 파라미터", A["OTHER_SPREAD"], V8_RESULT["기타 가산"]),
        ("AR(Gini)", "검증 표본", round(m["ar"], 4), V8_RESULT["AR"]),
        ("(참고) JB 외국인 평균 대출액(원)", "JB 공시", A["BENCH_AVG_LOAN"], A["BENCH_AVG_LOAN"]),
        ("(참고) SOL 공시 평균 금리 — 급여이체 우대만", "SOL 공시", round(A["BASE_RATE"] + A["DISCLOSED_SPREAD"] - A["PREF_SALARY"], 4), None),
    ], columns=["항목", "기준", "V9", "V8"])

    band_order = ["951~1000", "901~950", "851~900", "801~850", "751~800", "701~750", "651~700", "601~650", "600 이하"]
    band_dist = (df.groupby("score_band").agg(전체=("user_id", "size"), 소득자=("monthly_income", lambda x: (x > 0).sum()),
                                              평균PD=("pd", "mean"), 승인=("decision", lambda x: (x == "승인").sum()))
                 .reindex([b for b in band_order if b in set(df.score_band)]).round(4))
    rate_table = (ok.groupby("score_band").agg(승인인원=("user_id", "size"), 평균PD=("pd", "mean"),
                                               기준금리=("rate_base", "mean"), 가산금리=("rate_spread", "mean"),
                                               우대금리=("rate_preferential", "mean"), 최종금리=("rate", "mean"),
                                               최종금리_최저=("rate", "min"), 최종금리_최고=("rate", "max"))
                  .reindex([b for b in band_order if b in set(ok.score_band)]))
    rate_table.loc["전체"] = [len(ok), ok.pd.mean(), ok.rate_base.mean(), ok.rate_spread.mean(),
                            ok.rate_preferential.mean(), ok.rate.mean(), ok.rate.min(), ok.rate.max()]
    rate_table = rate_table.round(4)

    # 거절자 포함 금리 분포 (금리가 산출된 소득자 전체)
    rated = df[inc]
    rate_bins = pd.cut(rated.rate, [0, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 1.0],
                       labels=["10% 미만", "10~12%", "12~14%", "14~16%", "16~18%", "18~20%", "20% 초과(거절)"])
    rate_all = (rated.groupby(rate_bins, observed=False).agg(소득자=("user_id", "size"),
                                                             승인=("decision", lambda x: (x == "승인").sum()),
                                                             대상아님=("decision", lambda x: (x == "대상아님").sum()),
                                                             거절=("decision", lambda x: (x == "거절").sum()),
                                                             평균점수=("credit_score", "mean")).round(1))
    rate_all.index.name = "산출금리 구간 (소득자 전체)"

    def pref_label(r):
        parts_ = (["급여이체"] if r.shinhan_payroll else []) + (["청약"] if r.has_subscription else []) + (["대환"] if r.is_refinance else [])
        return "+".join(parts_) if parts_ else "우대 없음"
    pref_dist = (ok.assign(우대조합=ok.apply(pref_label, axis=1))
                 .groupby("우대조합").agg(인원=("user_id", "size"), 우대금리=("rate_preferential", "first"), 평균금리=("rate", "mean")).round(4))

    lim_band = (ok.groupby("score_band").agg(승인인원=("user_id", "size"), 평균금리=("rate", "mean"),
                                             평균대출기간=("loan_term", "mean"), 평균월소득=("monthly_income", "mean"),
                                             평균최소한도=("min_limit", "mean"), 평균최대한도=("max_limit", "mean"))
                .reindex([b for b in band_order if b in set(ok.score_band)]))
    lim_band["최대한도/연소득"] = (ok.groupby("score_band").apply(lambda g: (g.max_limit / (g.monthly_income * 12)).mean())
                              .reindex(lim_band.index))
    lim_band.loc["전체"] = [len(ok), ok.rate.mean(), ok.loan_term.mean(), ok.monthly_income.mean(),
                          ok.min_limit.mean(), ok.max_limit.mean(), (ok.max_limit / (ok.monthly_income * 12)).mean()]
    lim_band = lim_band.round(4)
    bind_tab = pd.DataFrame({"최대한도 결정요인": ok.max_limit_binding.value_counts(),
                             "최소한도 결정요인": ok.min_limit_binding.value_counts()}).fillna(0).astype(int)
    term_bucket = pd.cut(ok.loan_term, [5, 12, 24, 35, 36], labels=["6~12개월", "13~24개월", "25~35개월", "36개월"])
    lim_term = ok.groupby(term_bucket, observed=False).agg(인원=("user_id", "size"), 평균최대한도=("max_limit", "mean"),
                                                       평균최소한도=("min_limit", "mean")).round(0)
    lim_term.index.name = "대출기간"

    # 공정성 점검: 체류자격별 점수·승인율·금리·한도 (점수에 비자는 들어가지 않음)
    fair = (df.groupby("visa_type").agg(
        인원=("user_id", "size"), 소득자=("monthly_income", lambda x: (x > 0).sum()),
        평균점수=("credit_score", "mean"), 평균PD=("pd", "mean"),
        승인=("decision", lambda x: (x == "승인").sum()),
        대상아님=("decision", lambda x: (x == "대상아님").sum()), 거절=("decision", lambda x: (x == "거절").sum())))
    fair["승인율(소득자 대비)"] = fair["승인"] / fair["소득자"].replace(0, np.nan)
    ok_v = ok.groupby("visa_type").agg(승인자_평균금리=("rate", "mean"), 승인자_평균최대한도=("max_limit", "mean"),
                                       승인자_평균대출기간=("loan_term", "mean"))
    fair = fair.join(ok_v).sort_values("인원", ascending=False).round(4)
    fair_reason = (df[df.decision != "승인"].groupby(["visa_type", "reason"]).size().unstack(fill_value=0))
    remit_by_visa = df[inc].groupby("visa_type").agg(평균송금비율=("remit_ratio", "mean"),
                                                     송금자비율=("remit_ratio", lambda x: (x > 0).mean())).round(3)

    # 유학생 예시
    stu_grid = student_grid()
    stu_demo = df[df.visa_type == STUDENT].decision.value_counts().rename("인원").to_frame()

    dist = pd.DataFrame({
        "연체 횟수": range(7),
        "통신비 인원": [int((df.tel_late_6m == k).sum()) for k in range(7)],
        "건보·공과금 인원": [int((df.util_late_6m == k).sum()) for k in range(7)]})
    assumptions_df = pd.DataFrame([(k, str(v[0]), v[1]) for k, v in ASSUMPTIONS.items()], columns=["파라미터", "값", "근거"])

    cols = ["user_id", "visa_type", "stay_duration_months", "months_to_expiry", "wage_bracket", "monthly_income",
            "effective_monthly_income", "work_permitted", "salary_months", "shinhan_payroll",
            "tel_late_6m", "remit_ratio", "remit_months_6m", "remit_regularity", "util_late_6m", "pd", "credit_score", "score_band",
            "has_subscription", "is_refinance", "rate_base", "rate_credit_premium", "rate_capital_cost",
            "rate_other_spread", "rate_spread", "rate_preferential", "rate", "decision", "reason", "loan_term", "existing_debt",
            "cap_product", "cap_income80", "cap_payment", "cap_dsr", "max_limit_binding", "min_limit_binding", "min_limit", "max_limit"]
    with pd.ExcelWriter("외국인_대안신용평가_V9.xlsx", engine="openpyxl") as w:
        df[cols].to_excel(w, sheet_name="결과_1000명", index=False)
        bench.to_excel(w, sheet_name="V8대비비교", index=False)
        decision_tab.to_excel(w, sheet_name="판정요약", index=False)
        metrics.to_excel(w, sheet_name="모델검증", index=False)
        share_df.to_excel(w, sheet_name="모델검증", index=False, startrow=len(metrics) + 2)
        fair.to_excel(w, sheet_name="공정성점검(체류자격별)", startrow=1)
        fair_reason.to_excel(w, sheet_name="공정성점검(체류자격별)", startrow=len(fair) + 5)
        remit_by_visa.to_excel(w, sheet_name="공정성점검(체류자격별)", startrow=len(fair) + len(fair_reason) + 9)
        stu_grid.to_excel(w, sheet_name="유학생예시(월100만)", index=False, startrow=1)
        stu_demo.to_excel(w, sheet_name="유학생예시(월100만)", startrow=len(stu_grid) + 5)
        rate_table.to_excel(w, sheet_name="점수구간별금리(승인자)")
        rate_all.to_excel(w, sheet_name="금리분포(거절자포함)")
        pref_dist.to_excel(w, sheet_name="우대금리분포")
        lim_band.to_excel(w, sheet_name="점수구간별한도")
        bind_tab.to_excel(w, sheet_name="한도결정요인", startrow=1)
        lim_term.to_excel(w, sheet_name="한도결정요인", startrow=len(bind_tab) + 5)
        mono.to_excel(w, sheet_name="연체횟수별PD", startrow=1)
        mono_u.to_excel(w, sheet_name="연체횟수별PD", startrow=len(mono) + 5)
        mono_r.to_excel(w, sheet_name="송금비율별PD", startrow=1)
        mono_g.to_excel(w, sheet_name="송금규칙성별PD", startrow=1)
        dist.to_excel(w, sheet_name="연체분포", index=False)
        scorecard.to_excel(w, sheet_name="점수표", index=False)
        band_dist.to_excel(w, sheet_name="점수구간분포")
        NICE_BAND.to_excel(w, sheet_name="점수척도보정(NICE)", index=False)
        assumptions_df.to_excel(w, sheet_name="가정·출처", index=False)

    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    for title, t in [("V8 대비", bench), ("판정 요약", decision_tab), ("모델 검증", metrics), ("NICE 비중", share_df),
                     ("점수구간별 금리(승인자)", rate_table), ("금리 분포(거절자 포함)", rate_all), ("우대금리", pref_dist),
                     ("점수구간별 한도", lim_band), ("한도 결정요인", bind_tab), ("대출기간별 한도", lim_term),
                     ("공정성(체류자격별)", fair), ("체류자격별 비승인 사유", fair_reason), ("체류자격별 송금", remit_by_visa),
                     ("유학생 예시(월 100만, 신한 급여이체)", stu_grid), ("데모 유학생 판정", stu_demo), ("점수표", scorecard)]:
        print(f"== {title} =="); print(t.to_string()); print()
