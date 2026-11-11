"""E-7-4 점수 계산 테스트 화면(개발·규칙 검토용). 엔진(assess_e74)을 직접 부른다.

실행(저장소 루트): uv run --project ai --with streamlit streamlit run ai/playground/e74_app.py
API·DB·LLM 없이 동작한다. 합성 샘플만 쓰고, 실제 사용자 데이터를 넣지 않는다.
"""

from __future__ import annotations

import json
from datetime import date

import _bootstrap  # noqa: F401  (app 패키지 import 경로)
import pandas as pd
import streamlit as st

from app.visa.e74.engine import E74Assessment, EligibilityStatus
from playground.e74_views import (
    ITEM_GROUPS,
    STATUS_LABELS,
    comparison,
    improvement_rows,
    item_rows,
    load_samples,
    missing_rows,
    parse_profile,
    plan_text,
    run,
)

st.set_page_config(page_title="E-7-4 점수 계산 테스트", layout="wide")
SAMPLES = load_samples()


def _use_sample() -> None:
    st.session_state["profile_text"] = json.dumps(
        SAMPLES[st.session_state["sample"]], ensure_ascii=False, indent=2
    )


def _status_banner(assessment: E74Assessment) -> None:
    label = STATUS_LABELS[assessment.status]
    show = {
        EligibilityStatus.ELIGIBLE: st.success,
        EligibilityStatus.INELIGIBLE: st.error,
        EligibilityStatus.NEEDS_EVIDENCE: st.warning,
        EligibilityStatus.NEEDS_REVIEW: st.info,
    }[assessment.status]
    show(
        f"결과: {label} · 규칙 {assessment.rule_version or '없음'} · 기준일 {assessment.as_of_date}"
    )


def _summary(assessment: E74Assessment, previous: E74Assessment | None) -> None:
    diff = comparison(previous, assessment)
    cols = st.columns(5)
    cols[0].metric("확정 총점", assessment.score.total, diff["totalDelta"] if diff else None)
    cols[1].metric(
        "가능 총점",
        assessment.potential_score.total,
        diff["potentialDelta"] if diff else None,
        help="신고값까지 인정한 점수. 모르는 항목이 있으면 더 오를 수 있음",
    )
    cols[2].metric("기준 총점", assessment.score.required if assessment.score.required else "-")
    cols[3].metric("부족 점수", assessment.score_gap.points_needed or 0)
    cols[4].metric("한국어 유예", "적용" if assessment.korean_deferral_applied else "-")
    if diff:
        before, after = diff["status"]
        st.caption(f"직전 계산과 비교: 상태 {before} → {after}")
    st.caption(
        f"기본 {assessment.score.base} · 가점 {assessment.score.bonus} · "
        f"감점 {assessment.score.penalty} · requestKey {assessment.request_key[:16]}…"
    )


def main() -> None:
    st.title("E-7-4 점수 계산 테스트")
    names = list(SAMPLES)
    # 위젯을 만들기 전에 초기값을 넣어야 한다(만든 뒤에는 값을 바꿀 수 없음).
    if "sample" not in st.session_state:
        st.session_state["sample"] = names[0]
    if "profile_text" not in st.session_state:
        _use_sample()
    with st.sidebar:
        st.selectbox("샘플 프로필", names, key="sample", on_change=_use_sample)
        as_of = st.date_input("기준일(신청일)", value=date.today())
        include_draft = st.checkbox(
            "DRAFT 규칙 허용", value=True, help="2026 규칙은 팀 확인 전이라 DRAFT 상태"
        )
        st.caption("합성 데이터만 사용. 계산은 이 PC에서 엔진을 직접 호출한다.")

    left, right = st.columns([2, 3])
    with left:
        st.subheader("프로필 JSON")
        st.text_area("요청 본문(camelCase)", key="profile_text", height=640)

    profile, errors = parse_profile(st.session_state["profile_text"])
    with right:
        if profile is None:
            st.error("입력을 계산할 수 없음")
            for error in errors:
                st.write(f"- {error}")
            return
        assessment = run(profile, as_of, include_draft)
        previous: E74Assessment | None = st.session_state.get("previous")
        _status_banner(assessment)
        _summary(assessment, previous)

        st.subheader("개선 방안")
        st.write(plan_text(assessment))
        if assessment.improvements:
            st.dataframe(pd.DataFrame(improvement_rows(assessment)), hide_index=True)

        st.subheader(f"재질의 ({len(assessment.missing_inputs)})")
        if assessment.missing_inputs:
            st.dataframe(pd.DataFrame(missing_rows(assessment)), hide_index=True)
        else:
            st.write("묻을 것 없음")

        if assessment.review_notes:
            st.subheader("검토 필요")
            for note in assessment.review_notes:
                st.write(f"- {note.rule_code or '-'}: {note.note}")

        st.subheader("항목별 결과")
        tabs = st.tabs(list(ITEM_GROUPS))
        for tab, categories in zip(tabs, ITEM_GROUPS.values(), strict=True):
            with tab:
                st.dataframe(pd.DataFrame(item_rows(assessment, categories)), hide_index=True)

        with st.expander("원본 결과 JSON"):
            st.json(assessment.model_dump(mode="json", by_alias=True))

    if previous is None or previous.request_key != assessment.request_key:
        st.session_state["previous"] = assessment


main()
