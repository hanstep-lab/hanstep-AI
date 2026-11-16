"""대안신용 V9 모델을 JSON으로 고정하고 원본 결과 비교 픽스처를 만든다.

원본(``models/alt_credit/v9_source.py``, 신재현 V9 11/12 버전)은 고치지 않고 import만 한다.
원본의 ``__main__`` 블록(엑셀 저장)은 실행하지 않는다. 학습은 원본 ``__main__``과 같은
순서·seed로 한다: generate_population(60000, seed=7) → 소득자 → base_betas → solve_scale → fit.
sklearn·scipy는 이 스크립트에서만 쓴다(앱 의존성 아님). ``ai/``에서 실행한다.

    uv run --with scikit-learn --with scipy python scripts/build_alt_credit_v9.py

출력
- ``models/alt_credit/alt_credit_v9.json``: 서빙(``app/credit/alt_score.py``)이 읽는 계수·척도·가정
- ``tests/fixtures/alt_credit_v9_demo.json``: 원본 방식 데모 1,000명(seed=42)의 입력과 결과
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
import scipy
import sklearn

AI_ROOT = Path(__file__).resolve().parents[1]
SOURCE = AI_ROOT / "models" / "alt_credit" / "v9_source.py"
MODEL_OUT = AI_ROOT / "models" / "alt_credit" / "alt_credit_v9.json"
FIXTURE_OUT = AI_ROOT / "tests" / "fixtures" / "alt_credit_v9_demo.json"

MODEL_VERSION = "alt-credit-v9"
TRAIN_SIZE, TRAIN_SEED = 60000, 7
DEMO_SIZE, DEMO_SEED = 1000, 42

# 픽스처 열: 서빙 입력 → 원본이 만든 파생값 → 원본 결과(금리 구성·판정·한도·각 상한)
INPUT_COLUMNS = [
    "user_id",
    "visa_type",
    "stay_duration_months",
    "months_to_expiry",
    "monthly_income",
    "work_permitted",
    "salary_months",
    "shinhan_payroll",
    "has_subscription",
    "is_refinance",
    "tel_late_6m",
    "util_late_6m",
    "remit_ratio",
    "remit_months_6m",
    "existing_debt",
    "existing_monthly_payment",
]
DERIVED_COLUMNS = ["remit_regularity", "effective_monthly_income"]
RESULT_COLUMNS = [
    "pd",
    "credit_score",
    "rate_base",
    "rate_credit_premium",
    "rate_capital_cost",
    "rate_other_spread",
    "rate_spread",
    "rate_preferential",
    "rate",
    "decision",
    "reason",
    "loan_term",
    "min_limit",
    "max_limit",
    "max_limit_binding",
    "min_limit_binding",
    "cap_product",
    "cap_income80",
    "cap_payment",
    "cap_dsr",
]


def load_source() -> ModuleType:
    """원본을 모듈로 읽는다. 모듈 이름이 ``__main__``이 아니라 엑셀 저장 블록은 돌지 않는다."""
    sys.dont_write_bytecode = True  # 원본 폴더에 __pycache__를 만들지 않는다
    spec = importlib.util.spec_from_file_location("alt_credit_v9_source", SOURCE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain(value: Any) -> Any:
    """numpy 값을 JSON 값으로. NaN은 null."""
    if isinstance(value, np.bool_ | bool):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating | float):
        number = float(value)
        return None if math.isnan(number) else number
    if isinstance(value, tuple):
        return [plain(v) for v in value]
    return value


def remitter_neutral(population: pd.DataFrame) -> float:
    """송금 규칙성 중립값: 송금자(remit_ratio > 0)의 remit_months_6m 평균(원본과 같은 식)."""
    return float(population.loc[population["remit_ratio"] > 0, "remit_months_6m"].mean())


def train(v9: ModuleType) -> tuple[Any, dict[str, Any], float, float, pd.DataFrame]:
    """원본 ``__main__``의 학습 부분을 같은 순서로 실행한다."""
    population = v9.generate_population(TRAIN_SIZE, seed=TRAIN_SEED)
    earners = population[population.monthly_income > 0].reset_index(drop=True)
    betas, means = v9.base_betas(earners)
    scale = v9.solve_scale(earners, betas, means, v9.A["TARGET_BAD_RATE"], v9.A["TARGET_AR"])
    model, metrics = v9.fit(earners, betas, means, scale, v9.A["TARGET_BAD_RATE"])
    return model, metrics, float(scale), remitter_neutral(earners), earners


def score_demo(v9: ModuleType, model: Any) -> pd.DataFrame:
    """원본 ``__main__``의 데모 1,000명 계산(점수·금리·판정)을 그대로 따른다."""
    df = v9.generate_population(DEMO_SIZE, seed=DEMO_SEED)
    inc = df.monthly_income > 0
    df["pd"] = model.predict_proba(df[v9.FEATURES])[:, 1]
    df["credit_score"] = v9.pd_to_score(df["pd"])
    parts = v9.loan_rate(
        df["pd"].values,
        df["has_subscription"].values,
        df["is_refinance"].values,
        df["shinhan_payroll"].values,
    )
    for k, v in parts.items():
        df[k] = np.where(inc, v, np.nan)
    return pd.concat([df, df.apply(v9.decide, axis=1)], axis=1)


def model_document(
    v9: ModuleType,
    model: Any,
    metrics: dict[str, Any],
    scale: float,
    neutral: float,
    earners: pd.DataFrame,
    source_sha256: str,
) -> dict[str, Any]:
    if list(model.classes_) != [0, 1]:
        raise SystemExit(f"unexpected classes: {model.classes_}")
    return {
        "model_version": MODEL_VERSION,
        "source": {"file": "models/alt_credit/v9_source.py", "sha256": source_sha256},
        "features": list(v9.FEATURES),
        "coef": [float(c) for c in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "score_scale": {"offset": float(v9.offset), "factor": float(v9.factor)},
        "remit_regularity_neutral": neutral,
        # 학습 모집단(소득자) 변수별 [최솟값, 최댓값].
        # 서빙은 누적 체류가 이 범위 밖이면 계산하지 않는다.
        "feature_ranges": {
            name: [plain(earners[name].min()), plain(earners[name].max())] for name in v9.FEATURES
        },
        "assumptions": {
            name: {"value": plain(value), "basis": basis}
            for name, (value, basis) in v9.ASSUMPTIONS.items()
        },
        "training": {
            "population_size": TRAIN_SIZE,
            "population_seed": TRAIN_SEED,
            "earners": len(earners),
            "remitters": int((earners["remit_ratio"] > 0).sum()),
            "n_train": int(metrics["n_train"]),
            "n_test": int(metrics["n_test"]),
            "fit_seed": 11,  # 원본 fit()의 기본 seed(부도 표본 추출)
            "target_bad_rate": float(v9.A["TARGET_BAD_RATE"]),
            "target_ar": float(v9.A["TARGET_AR"]),
            "coef_scale": scale,
            "true_pd_intercept": float(metrics["alpha"]),
            "auc": float(metrics["auc"]),
            "ar": float(metrics["ar"]),
            "ks": float(metrics["ks"]),
            "test_bad_rate": float(metrics["test_bad"]),
            "test_mean_pred_pd": float(metrics["pred_pd"]),
            "logistic_regression": {k: plain(v) for k, v in model.get_params().items()},
            "versions": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "scipy": scipy.__version__,
                "scikit-learn": sklearn.__version__,
            },
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }


def fixture_text(demo: pd.DataFrame, meta: dict[str, Any]) -> str:
    """한 줄에 한 명씩 쓴다(열 이름은 columns 한 번만)."""
    columns = INPUT_COLUMNS + DERIVED_COLUMNS + RESULT_COLUMNS
    rows = [
        json.dumps([plain(v) for v in record], ensure_ascii=False)
        for record in demo[columns].itertuples(index=False, name=None)
    ]
    return (
        "{\n"
        f'  "meta": {json.dumps(meta, ensure_ascii=False)},\n'
        f'  "columns": {json.dumps(columns)},\n'
        '  "rows": [\n    ' + ",\n    ".join(rows) + "\n  ]\n}\n"
    )


def main() -> int:
    if not SOURCE.is_file():
        raise SystemExit(f"original model file is missing: {SOURCE}")
    source_sha256 = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    v9 = load_source()

    started = time.perf_counter()
    model, metrics, scale, neutral, earners = train(v9)
    trained = time.perf_counter()
    demo = score_demo(v9, model)
    finished = time.perf_counter()

    # 원본은 비송금자의 규칙성을 round(중립값, 3)으로 채운다. 학습 모집단과 같은 값인지 확인한다.
    filled = earners.loc[earners["remit_ratio"] == 0, "remit_regularity"].unique()
    if not (filled == np.round(neutral, 3)).all():
        raise SystemExit(f"neutral mismatch: {filled} vs {neutral}")

    document = model_document(v9, model, metrics, scale, neutral, earners, source_sha256)
    MODEL_OUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    demo_neutral = remitter_neutral(demo)
    meta = {
        "model_version": MODEL_VERSION,
        "source_sha256": source_sha256,
        "demo_size": DEMO_SIZE,
        "demo_seed": DEMO_SEED,
        "earners": int((demo["monthly_income"] > 0).sum()),
        # 원본 데모는 데모 모집단의 송금자 평균으로 비송금자를 채운다(학습 중립값과 다름).
        "remit_regularity_neutral_demo": demo_neutral,
        "remit_regularity_neutral_training": neutral,
        "generated_by": "scripts/build_alt_credit_v9.py",
    }
    FIXTURE_OUT.write_text(fixture_text(demo, meta), encoding="utf-8", newline="\n")

    summary = {
        "model": MODEL_OUT.relative_to(AI_ROOT).as_posix(),
        "fixture": FIXTURE_OUT.relative_to(AI_ROOT).as_posix(),
        "auc": round(float(metrics["auc"]), 4),
        "ar": round(float(metrics["ar"]), 4),
        "ks": round(float(metrics["ks"]), 4),
        "neutral_training": neutral,
        "neutral_demo": demo_neutral,
        "decisions": demo["decision"].value_counts().to_dict(),
        "seconds_training": round(trained - started, 2),
        "seconds_demo": round(finished - trained, 2),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
