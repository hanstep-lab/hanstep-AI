"""scripts/_lang_check.py: 답변 언어 검사(#106)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _lang_check as LC  # noqa: E402

pytestmark = pytest.mark.unit

KOREAN_E74 = (
    "현재 E-7-4 확정 점수는 172점이고 기준은 200점입니다. 한국어를 TOPIK 4급으로 올리면 "
    "+40점을 받을 수 있습니다. 이 결과는 사전 점검이며 실제 비자 결정이 아닙니다."
)
ENGLISH_E74 = (
    "Your confirmed E-7-4 score is 172 points and the requirement is 200. Raising your Korean "
    "to TOPIK level 4 would add 40 points. This is a pre-check, not a visa decision."
)


def test_english_answer_is_clean() -> None:
    check = LC.language_check(ENGLISH_E74, "en")
    assert check["mixed"] is False and check["mixedRaw"] is False
    assert check["inLanguage"] is True and check["mostlyKorean"] is False
    assert check["hangulShare"] == 0.0


def test_korean_answer_for_english_user_is_mixed_and_mostly_korean() -> None:
    check = LC.language_check(KOREAN_E74, "en")
    assert check["mixed"] is True and check["mostlyKorean"] is True
    assert check["inLanguage"] is False
    assert "현재" in check["disallowedRuns"]


def test_one_korean_phrase_is_mixed_but_not_mostly_korean() -> None:
    answer = ENGLISH_E74 + " Submit 세무서 발행 소득금액증명원 for income."
    check = LC.language_check(answer, "en")
    assert check["mixed"] is True and check["mostlyKorean"] is False
    assert check["disallowedRuns"] == ["세무서 발행 소득금액증명원"]


def test_allowlisted_official_name_is_not_mixed() -> None:
    answer = (
        "Under the Foreign Exchange Transaction Regulation (외국환거래규정) and the "
        "외국환거래법 시행령, you need a designated bank."
    )
    check = LC.language_check(answer, "en")
    assert check["mixedRaw"] is True
    assert check["mixed"] is False and check["inLanguage"] is True
    assert check["hangulRuns"] == ["외국환거래규정", "외국환거래법 시행령"]


def test_gloss_is_only_a_short_korean_term_alone_in_brackets() -> None:
    answer = "You need a designated bank (지정거래외국환은행) and a ( 외국인거주자 )."
    check = LC.language_check(answer, "en")
    assert check["mixed"] is True and check["mixedBare"] is False
    assert check["bareRuns"] == []
    # 따옴표 안, 한글 뒤 괄호에 영어, 괄호 안에 다른 글자가 함께 있으면 병기가 아니다.
    for text in (
        'Check the FINE menu "소액해외송금업 등록업체 조회" first.',
        "You are a 외국인거주자 (foreign resident) here.",
        "See the rule (Article 2, 외국인거주자).",
        "A foreign resident (Article 2) is 외국인거주자 here.",
    ):
        assert LC.language_check(text, "en")["mixedBare"] is True, text
    # 한글 20자 초과, 조사·문장 끝이 있으면 병기가 아니다.
    long_term = "가" * 21
    assert LC.language_check(f"A ({long_term}).", "en")["mixedBare"] is True
    assert LC.language_check(f"A ({'가' * 20}).", "en")["mixedBare"] is False
    assert LC.language_check("A (지정거래외국환은행을 통해).", "en")["mixedBare"] is True
    assert LC.language_check("A (은행에 신고해야 합니다).", "en")["mixedBare"] is True
    # 명사 끝과 겹치는 글자(한도)는 조사로 보지 않는다.
    assert LC.language_check("The limit (지급 한도) applies.", "en")["mixedBare"] is False
    assert LC.language_check(KOREAN_E74, "en")["mixedBare"] is True


def test_improvement_check_on_english_only() -> None:
    answer = "Raise your Korean to TOPIK level 4 for +40 points, and submit your tax certificate."
    check = LC.improvement_check(answer, "en")
    assert check == {
        "auto": True,
        "mentioned": True,
        "korean": True,
        "points": True,
        "documents": True,
    }
    none = LC.improvement_check("Your score is 172 points. This is a pre-check.", "en")
    assert none["mentioned"] is False
    assert LC.improvement_check(answer, "vi")["mentioned"] is None


def test_improvement_lines_table_only_with_answers() -> None:
    rows = [
        {
            "variant": "v4.4",
            "questionId": "LE03",
            "repeat": 1,
            "language": "en",
            "answer": "Get TOPIK 4 | for points.",
            "improvementCheck": LC.improvement_check("Get TOPIK 4.", "en"),
        },
        {"variant": "v4.4", "questionId": "LE01", "repeat": 1, "answer": "x"},
    ]
    text = "\n".join(LC.improvement_lines(rows, answers=True))
    assert "자동 판정(en): 언급 1/1" in text
    assert "| v4.4 LE03#1 | en | O | O | X | X |  | Get TOPIK 4 \\| for points. |" in text
    assert "| v4.4 LE03#1" not in "\n".join(LC.improvement_lines(rows, answers=False))
    assert LC.improvement_lines([rows[1]], answers=True) == []


def test_particle_on_official_name_is_mixed() -> None:
    check = LC.language_check("See 외국환거래규정에 따라 the rule.", "en")
    assert check["disallowedRuns"] == ["외국환거래규정에 따라"]


def test_product_names_and_parts_are_allowed() -> None:
    check = LC.language_check("Try 신한 땡겨요 적금, an installment savings (적금).", "en")
    assert check["mixed"] is False


def test_hangul_from_the_question_is_allowed() -> None:
    answer = "Do not lend your 체크카드 to anyone."
    assert LC.language_check(answer, "en")["mixed"] is True
    check = LC.language_check(answer, "en", ["i gave my 체크카드 to a guy"])
    assert check["mixed"] is False


def test_vietnamese_answer_passes_and_english_fallback_fails() -> None:
    vi = "Điểm E-7-4 hiện tại của bạn là 172 điểm, tiêu chuẩn là 200 điểm."
    assert LC.language_check(vi, "vi")["inLanguage"] is True
    assert LC.language_check(ENGLISH_E74, "vi")["inLanguage"] is False
    mixed_vi = vi + " 한국어를 올리세요."
    assert LC.language_check(mixed_vi, "vi")["inLanguage"] is False


def test_korean_user_expects_korean() -> None:
    check = LC.language_check(KOREAN_E74, "ko")
    assert check["inLanguage"] is True and check["mixed"] is False
    assert check["disallowedRuns"] == []
    assert LC.language_check(ENGLISH_E74, "ko")["inLanguage"] is False


def test_row_check_uses_saved_check_or_recomputes_as_english() -> None:
    assert LC.row_check({"status": "error", "answer": KOREAN_E74}) is None
    old = {"status": "done", "answer": KOREAN_E74, "contextTexts": ["What's my E-7-4 score?"]}
    assert LC.row_check(old)["language"] == "en"
    assert LC.row_check(old)["mixed"] is True
    saved = {"status": "done", "answer": KOREAN_E74, "languageCheck": {"language": "ko"}}
    assert LC.row_check(saved) == {"language": "ko"}


def test_row_check_joins_follow_up_answer() -> None:
    row = {
        "status": "done",
        "answer": "Who should receive it?",
        "followUp": {"answer": "엄마에게 보낼게요."},
    }
    assert LC.row_check(row)["mixed"] is True


def test_summary_lines_counts_per_question() -> None:
    rows = [
        {"status": "done", "questionId": "LE01", "answer": KOREAN_E74, "language": "en"},
        {"status": "done", "questionId": "LE01", "answer": ENGLISH_E74, "language": "en"},
        {"status": "error", "questionId": "LE01", "answer": "", "language": "en"},
    ]
    text = "\n".join(LC.summary_lines(rows))
    assert "사용자 언어 en: 한글 혼입(허용 목록 적용) 1/2" in text
    assert "| LE01 | en | 2 | 1/2 | 1/2 | 1/2 | 1/2 |" in text
    assert LC.summary_lines([]) == []


def test_allowlist_file_loads_terms_and_product_names() -> None:
    terms = LC.allowed_terms()
    assert "외국환거래규정" in terms and "하이코리아" in terms
    assert "신한 땡겨요 적금" in terms
