"""E-7-4 점수 계산·채팅 테스트 화면(개발·규칙 검토용).

실행(저장소 루트): uv run --project ai --with streamlit streamlit run ai/playground/e74_app.py
- 점수 계산 탭: 엔진(assess_e74)을 직접 부른다. API·DB·LLM 없이 동작한다.
- 채팅 탭: Claude(`AGENT_MODEL`)가 정책 문서 검색(테스트 인덱스)과 점수 계산을 도구로 쓴다.
  Anthropic API 비용이 든다. 검색 인덱스는 이 PC 메모리에만 있다(승인 전 문서).
합성 샘플만 쓰고, 실제 사용자 데이터를 넣지 않는다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import _bootstrap  # noqa: F401  (app 패키지 import 경로)
import anthropic
import pandas as pd
import streamlit as st

from app.core.config import get_settings
from app.rag.embeddings import BgeM3Embedder
from app.visa.e74.engine import E74Assessment, EligibilityStatus
from app.visa.e74.schemas import E74Profile
from playground.e74_chat import PROMPT_ID, ChatTurn, SearchFn, ToolContext, run_turn
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
from playground.rag_test_index import TestIndex, build_index, search_payload

st.set_page_config(page_title="E-7-4 점수 계산 테스트", layout="wide")
SAMPLES = load_samples()


def _use_sample() -> None:
    st.session_state["profile_text"] = json.dumps(
        SAMPLES[st.session_state["sample"]], ensure_ascii=False, indent=2
    )


# --- 점수 계산 탭 -------------------------------------------------------------------------


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


def _render_score(
    profile: E74Profile | None, errors: list[str], as_of: date, include_draft: bool
) -> None:
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


# --- 채팅 탭 -----------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _embedder() -> BgeM3Embedder:
    settings = get_settings()
    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    embedder.load()  # 처음에는 Hugging Face에서 약 2.2GB를 받는다
    return embedder


@st.cache_resource(show_spinner=False)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key.get_secret_value())


def _build_index() -> TestIndex:
    with st.status("검색 인덱스 만드는 중", expanded=True) as status:
        status.write("BGE-M3 모델 불러오기(처음이면 내려받기)")
        embedder = _embedder()
        tokenizer = embedder.load().tokenizer
        index = build_index(
            embedder,
            token_counter=lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
            progress=status.write,
        )
        status.update(label=f"검색 인덱스 준비됨: 청크 {len(index.chunks)}개", state="complete")
    return index


def _show_turn(entry: dict[str, Any]) -> None:
    with st.chat_message(entry["role"]):
        st.markdown(entry["text"] or "_(빈 답변)_")
        turn: ChatTurn | None = entry.get("turn")
        if turn is None:
            return
        if turn.notice:
            st.warning(turn.notice)
        for call in turn.tool_calls:
            label = f"도구 {call.name}({json.dumps(call.input, ensure_ascii=False)})"
            with st.expander(("⚠ " if call.is_error else "") + label):
                try:
                    st.json(json.loads(call.output))
                except json.JSONDecodeError:
                    st.write(call.output)
        st.caption(
            f"입력 {turn.input_tokens:,} · 출력 {turn.output_tokens:,} 토큰 · "
            f"종료 {turn.stop_reason}"
        )


def _render_chat(profile: E74Profile | None, include_draft: bool) -> None:
    settings = get_settings()
    st.caption(
        f"모델 {settings.agent_model} · 프롬프트 {PROMPT_ID} · "
        "검색: 테스트 인덱스(승인 전 문서, 이 PC 메모리) · 점수: 왼쪽 프로필로 엔진 계산"
    )
    log: list[dict[str, Any]] = st.session_state.setdefault("chat_log", [])
    history: list[dict[str, Any]] = st.session_state.setdefault("chat_history", [])
    index: TestIndex | None = st.session_state.get("rag_index")

    cols = st.columns([1, 1, 3])
    if cols[0].button("검색 인덱스 만들기", disabled=index is not None):
        st.session_state["rag_index"] = index = _build_index()
    if cols[1].button("대화 지우기"):
        log.clear()
        history.clear()
    if index is not None:
        documents = len({chunk.path for chunk in index.chunks})
        skipped = ", ".join(path for path, _ in index.skipped) or "없음"
        cols[2].caption(
            f"인덱스: 문서 {documents}개 · 청크 {len(index.chunks)}개"
            f"{' (캐시 사용)' if index.cache_hit else ''} · 제외: {skipped}"
        )
    else:
        cols[2].caption("인덱스가 없으면 정책 검색 도구가 오류를 돌려준다(점수 계산은 동작)")

    if not settings.anthropic_configured():
        st.error("ANTHROPIC_API_KEY가 설정되지 않음(ai/.env)")
        return

    for entry in log:
        _show_turn(entry)

    question = st.chat_input("E-7-4 관련 질문을 입력하세요")
    if not question:
        return
    log.append({"role": "user", "text": question})
    search: SearchFn | None = None
    if index is not None:
        embedder, ready = _embedder(), index
        search = lambda query, top_k: search_payload(ready, embedder, query, top_k)  # noqa: E731
    ctx = ToolContext(profile=profile, include_draft=include_draft, search=search)
    try:
        with st.spinner("답변 생성 중"):
            turn = run_turn(_client(), settings.agent_model, history, question, ctx)
        log.append({"role": "assistant", "text": turn.text, "turn": turn})
    except anthropic.APIStatusError as exc:
        history.pop()  # 실패한 질문은 대화 기록에서 뺀다
        log.append({"role": "assistant", "text": f"API 오류 {exc.status_code}: {exc.message}"})
    except anthropic.APIConnectionError:
        history.pop()
        log.append({"role": "assistant", "text": "네트워크 오류로 Anthropic API에 연결하지 못함"})
    st.rerun()


# --- 화면 --------------------------------------------------------------------------------


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
        score_tab, chat_tab = st.tabs(["점수 계산", "채팅"])
        with score_tab:
            _render_score(profile, errors, as_of, include_draft)
        with chat_tab:
            _render_chat(profile, include_draft)


main()
