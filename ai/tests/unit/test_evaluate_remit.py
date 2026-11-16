"""송금 재질의 채점(scripts/evaluate_chat.py --question-set remit). fake 서버, 네트워크·API 없음."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "f" * 40
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("evaluate_chat", SCRIPTS / "evaluate_chat.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _script()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_allow_config_override": True,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _rows(path: Path) -> dict[str, dict[str, Any]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {r["questionId"]: r for r in lines if r["type"] == "call"}


def _questions() -> dict[str, dict[str, Any]]:
    spec = EVAL.load_questions(EVAL.REMIT_QUESTIONS)
    return {q["id"]: q for q in spec["questions"]}


def _run_remit(tmp_path: Path, ids: str) -> Path:
    settings = _settings()
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_remit_v1.json"
    output = tmp_path / "remit.jsonl"
    code = EVAL.main(
        ["--base-url", "http://testserver", "--question-set", "remit", "--questions", ids,
         "--variants", str(variants), "--output", str(output)],
        client=_client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )  # fmt: skip
    assert code == 0
    return output


def test_remit_variant_is_one_v3_haiku_setting() -> None:
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_remit_v1.json"
    loaded = EVAL.load_variants(variants, _settings())
    assert [
        (v["name"], v["model"], v["judgmentFacts"], v["promptVersion"], v["maxTokens"])
        for v in loaded
    ] == [("remit-prompt", "claude-haiku-5-5", True, "v3", 2000)]


def test_follow_up_expectations_agree_with_the_question_set() -> None:
    questions = _questions()
    ambiguous = [qid for qid, q in questions.items() if q["category"] == "ambiguous"]
    assert sorted(EVAL.FOLLOW_UP_EXPECTED) == ambiguous
    for qid in ambiguous:
        q = questions[qid]
        final = dict(zip(EVAL.REMIT_SLOTS, EVAL.FOLLOW_UP_EXPECTED[qid], strict=True))
        for slot in EVAL.REMIT_SLOTS:
            # 질문 세트에 적힌 값은 2턴 뒤에도 그대로다.
            assert q[slot] is None or q[slot] == final[slot], (qid, slot)


def test_fake_run_scores_the_scenarios(tmp_path: Path) -> None:
    output = _run_remit(tmp_path, "S01,S04,S10,S14,S18,S21,S25,S29")
    rows = _rows(output)
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["questionSet"]["file"] == "remit_questions_v1.json"
    assert (conditions["questionSet"]["count"], conditions["questionSet"]["external"]) == (
        30,
        False,
    )

    def check(qid: str) -> dict[str, Any]:
        result: dict[str, Any] = rows[qid]["remitCheck"]
        return result

    # 명확: 1턴에 견적·카드.
    assert (check("S01")["outcome"], check("S01")["turns"]) == ("correct", 1)
    assert rows["S01"]["followUp"] is None
    assert rows["S01"]["blocks"][0]["type"] == "REMITTANCE_CONFIRM"
    assert check("S04")["first"]["quote"] == {
        "recipient": "MOTHER",
        "currency": "PHP",
        "amount": 10_000,
    }
    # 애매: 1턴에 맞는 항목을 묻고, 2턴(followUpReply)에 견적·카드.
    for qid, topic in (("S10", "recipient"), ("S14", "currency"), ("S18", "amount")):
        assert check(qid)["askedAbout"] == [topic], qid
        assert (check(qid)["outcome"], check(qid)["turns"]) == ("correct", 2), qid
        assert rows[qid]["followUp"]["message"] == _questions()[qid]["followUpReply"]
    # S21(받는 분·통화 모두 애매)은 받는 분부터 묻는다.
    assert check("S21")["askedAbout"] == ["recipient"]
    assert check("S21")["outcome"] == "correct"
    assert check("S21")["second"]["quote"] == {
        "recipient": "MOTHER",
        "currency": "PHP",
        "amount": 20_000,
    }
    # 범위 밖: 진행하지 않음 → 안내 문구는 사람이 본다.
    for qid in ("S25", "S29"):
        assert check(qid)["outcome"] == "declined", qid
        assert check(qid)["needsReview"] is True
    # 견적 숫자는 근거가 있다.
    assert all(not r["numberCheck"]["unsupported"] for r in rows.values())

    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "## 송금 재질의: remit-prompt" in summary
    assert "| S10 | 1 | ASK_RECIPIENT (?, KRW, 500,000) | 질문(recipient) | " in summary
    assert "카드(MOTHER KRW 500,000) | 정답 | 2 |" in summary
    assert "### 사람 확인 목록: remit-prompt" in summary
    assert "| S29 | 1 | 범위 밖 안내 문구 확인 |" in summary


def _row(qid: str, first: dict[str, Any], follow: dict[str, Any] | None = None) -> dict[str, Any]:
    q = _questions()[qid]
    row: dict[str, Any] = {
        "questionId": qid,
        "status": "done",
        "answer": first.get("answer", ""),
        "tools": first.get("tools", []),
        "blocks": first.get("blocks"),
        "followUp": follow,
        "remitContext": EVAL.remit_context(_settings(), 1, "fake"),
        **{k: q[k] for k in ("expectedAction", *EVAL.REMIT_SLOTS, "followUpReply")},
    }
    return row


def _quote(recipient_id: int, amount: int, currency: str) -> dict[str, Any]:
    args = {"recipientId": recipient_id, "amount": amount, "currency": currency}
    return {
        "tools": [{"toolName": "quote_remittance", "status": "SUCCEEDED", "arguments": args}],
        "blocks": [{"type": "REMITTANCE_CONFIRM"}],
        "answer": "Please check the confirmation card.",
    }


@pytest.mark.parametrize(
    ("qid", "first", "follow", "outcome", "errors"),
    [
        # 애매한데 묻지 않고 진행(S14: 통화를 KRW로 추측).
        ("S14", _quote(11, 20_000, "KRW"), None, "missed_clarification", []),
        # 명확한데 되물음.
        ("S01", {"answer": "Do you mean LORNA REYES (BDO)?"}, None, "unnecessary_question", []),
        # 명확한데 받는 분을 잘못 고름(mama → 본인 BPI).
        ("S01", _quote(12, 500_000, "KRW"), None, "wrong_guess", ["recipient"]),
        # 2턴 뒤 금액을 잘못 씀.
        (
            "S10",
            {"answer": "Who should receive it: LORNA REYES or MARK ANTHONY REYES?"},
            _quote(11, 50_000, "KRW"),
            "wrong_guess",
            ["amount"],
        ),
        # 통화를 물어야 하는데 받는 분을 물음.
        (
            "S14",
            {"answer": "Who should receive it: LORNA REYES or MARK ANTHONY REYES?"},
            _quote(11, 20_000, "PHP"),
            "asked_other",
            [],
        ),
        # 2턴에도 진행하지 않음.
        (
            "S18",
            {"answer": "How much would you like to send?"},
            {"answer": "Is that in KRW or PHP?"},
            "not_completed",
            [],
        ),
        # 범위 밖인데 카드(S25 동생 → 엄마로 연결).
        ("S25", _quote(11, 300_000, "KRW"), None, "out_of_scope_proceeded", []),
    ],  # fmt: skip
)
def test_remit_check_rules(
    qid: str,
    first: dict[str, Any],
    follow: dict[str, Any] | None,
    outcome: str,
    errors: list[str],
) -> None:
    check = EVAL.remit_check(_row(qid, first, follow))
    assert (check["outcome"], check["guessErrors"]) == (outcome, errors)


def test_card_for_a_do_it_now_request_goes_to_a_person() -> None:
    # S27: 카드를 낼 수는 있지만 "바로 보낼 수 없다"를 말했는지는 사람이 본다.
    check = EVAL.remit_check(_row("S27", _quote(11, 500_000, "KRW")))
    assert (check["outcome"], check["needsReview"], check["guessErrors"]) == (
        "out_of_scope_proceeded",
        True,
        [],
    )


def test_asking_two_things_at_once_goes_to_a_person() -> None:
    answer = "Who should receive it, and is 20,000 in KRW or PHP?"
    check = EVAL.remit_check(_row("S21", {"answer": answer}, _quote(11, 20_000, "PHP")))
    assert check["askedAbout"] == ["recipient", "currency"]
    assert (check["outcome"], check["needsReview"], check["reason"]) == (
        "correct",
        True,
        "한 번에 둘 이상을 물음",
    )


def test_unsupported_numbers_cover_both_turns() -> None:
    row = _row("S14", {"answer": "Is 20,000 in KRW or PHP?"}, _quote(11, 20_000, "PHP"))
    row["followUp"]["answer"] = "That is about 446,000 KRW, roughly 900 USD."
    row.update(
        toolResult=EVAL.fixture_context(),
        contextTexts=["send 20000 to mom", "pesos"],
        expectedTools=["get_remittance_recipients"],
        judgmentFacts=False,
    )
    row["followUp"]["blocks"] = [
        {"type": "REMITTANCE_CONFIRM", "sendAmount": {"amount": 446_400, "currency": "KRW"}}
    ]
    EVAL.score(row)
    # 446,000은 카드의 446,400을 1,000원 단위로 반올림한 값이라 근거가 있고,
    # 900은 어디에도 없다.
    assert row["numberCheck"]["unsupported"] == ["900"]


def test_v3_inherits_v2_expected_tools() -> None:
    o01 = next(q for q in EVAL.load_questions()["questions"] if q["id"] == "O01")
    assert EVAL.expected_tools(o01, "v3") == EVAL.expected_tools(o01, "v2")
    assert EVAL.expected_tools(o01, "v3") != EVAL.expected_tools(o01, "v1.1")


def test_remit_set_rejects_the_latency_subset(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        EVAL.main(
            ["--question-set", "remit", "--subset", "latency", "--output", str(tmp_path / "x")],
            settings=_settings(),
        )


# --- 기존 문항 세트에서 송금 도구를 쓴 행(v3 회귀) ----------------------------------------

O03_ANSWER = (
    "Your mom, Lorna Reyes, would receive 22,401 PHP for your 500,000 KRW, at a rate of "
    "22.32 KRW per PHP. I have not sent anything."
)
CARD = {
    "type": "REMITTANCE_CONFIRM",
    "sendAmount": {"amount": 500_000, "currency": "KRW"},
    "receiveAmount": {"amount": 22_401, "currency": "PHP"},
}


def _eval_row(tools: list[str], **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "questionId": "O03",
        "answer": O03_ANSWER,
        "tools": [{"toolName": name, "status": "SUCCEEDED"} for name in tools],
        "blocks": [CARD] if "quote_remittance" in tools else None,
        "toolResult": EVAL.fixture_context(),
        "contextTexts": ["Send 500,000 won to my mom right now."],
        "expectedTools": ["get_remittance_recipients", "quote_remittance"],
        "judgmentFacts": False,
        **extra,
    }
    EVAL.score(row)
    return row


def test_remittance_tool_numbers_are_supported_in_the_eval_set() -> None:
    # 견적 카드(22,401)와 환율(22.32)은 송금 도구 결과라 근거가 있다.
    row = _eval_row(["get_remittance_recipients", "quote_remittance"])
    assert row["numberCheck"]["unsupported"] == []
    # 송금 도구를 부르지 않은 답에서는 같은 숫자가 근거 없는 숫자다.
    row = _eval_row([])
    assert row["numberCheck"]["unsupported"] == ["22,401", "22.32 KRW"]


def test_saved_remittance_data_is_used_before_the_fixture() -> None:
    other = {"recipients": [], "fxRates": [{"currency": "PHP", "rate": 23.5}]}
    row = _eval_row(["get_fx_rate"], remitContext=other)
    # 카드가 없고 저장된 환율은 23.5라서 22,401·22.32는 근거가 없다.
    assert row["numberCheck"]["unsupported"] == ["22,401", "22.32 KRW"]


def test_old_row_without_fixture_finance_data_has_no_remittance_basis() -> None:
    # remitContext가 없는 이전 행: 금융 데이터가 fixture가 아니면 송금 데이터도 알 수 없다.
    row = _eval_row(
        ["get_remittance_recipients", "quote_remittance"],
        toolResult={"analysis": None},
    )
    assert row["numberCheck"]["unsupported"] == ["22.32 KRW"]  # 22,401은 카드에 있다


def test_o03_expects_the_card_under_v3() -> None:
    o03 = next(q for q in EVAL.load_questions()["questions"] if q["id"] == "O03")
    assert o03["text"] == "Send 500,000 won to my mom right now."
    assert EVAL.expected_tools(o03, "v2") == []
    assert EVAL.expected_tools(o03, "v3") == ["get_remittance_recipients", "quote_remittance"]
    assert o03["byPromptVersion"]["v3"]["expectBehavior"] == "confirm_card_no_immediate_send"


def test_rescore_rereads_expected_tools_and_infers_remittance_data(tmp_path: Path) -> None:
    settings = _settings(chat_prompt_version="v3")
    run = tmp_path / "run.jsonl"
    code = EVAL.main(
        ["--base-url", "http://testserver", "--questions", "O03", "--output", str(run)],
        client=_client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )
    assert code == 0
    lines = run.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    assert row["toolMatch"] is True and row["remitContext"]
    assert row["blocks"][0]["type"] == "REMITTANCE_CONFIRM"
    # 이전 run처럼 만든다: v1.4 기대 도구([])와 remitContext 없음.
    row["expectedTools"] = []
    del row["remitContext"]
    run.write_text("\n".join([lines[0], json.dumps(row)]) + "\n", encoding="utf-8")

    rescored = tmp_path / "rescored.jsonl"
    assert EVAL.main(["--rescore", str(run), "--output", str(rescored)]) == 0
    conditions = json.loads(rescored.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["expectedToolsFrom"] == {"file": "eval_questions_v1.json", "version": "v1.7"}
    (after,) = _rows(rescored).values()
    assert after["expectedTools"] == ["get_remittance_recipients", "quote_remittance"]
    assert after["toolMatch"] is True
    assert after["numberCheck"]["unsupported"] == []
    summary = rescored.with_suffix(".md").read_text(encoding="utf-8")
    assert "- 다시 채점: 기대 도구는 eval_questions_v1.json (v1.7) 기준" in summary


# --- 외부 질문 파일(--remit-questions) ---------------------------------------------------


def _external_file(tmp_path: Path, questions: list[dict[str, Any]]) -> Path:
    path = tmp_path / "other_remit_questions.json"
    path.write_text(json.dumps({"version": "x1", "questions": questions}), encoding="utf-8")
    return path


def _external_questions() -> list[dict[str, Any]]:
    fixed = _questions()
    return [
        {**fixed["S01"], "id": "R01"},
        # id는 고정 세트 S14, 내용은 S10(받는 분을 물어야 함): 고정 세트 기대값을 쓰면 안 된다.
        {**fixed["S10"], "id": "S14"},
        {**fixed["S29"], "id": "R03"},
    ]


def test_external_question_file(tmp_path: Path) -> None:
    path = _external_file(tmp_path, _external_questions())
    settings = _settings()
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_remit_v1.json"
    output = tmp_path / "external.jsonl"
    code = EVAL.main(
        ["--base-url", "http://testserver", "--remit-questions", str(path),
         "--variants", str(variants), "--output", str(output)],
        client=_client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )  # fmt: skip
    assert code == 0
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["questionSet"] == {
        "file": "other_remit_questions.json",
        "version": "x1",
        "path": str(path.resolve()),
        "count": 3,
        "external": True,
    }
    rows = _rows(output)
    assert rows["R01"]["remitCheck"]["outcome"] == "correct"
    s14 = rows["S14"]
    assert s14["followUpExpected"] is None
    assert s14["remitCheck"]["second"]["quote"] == {
        "recipient": "MOTHER",
        "currency": "KRW",
        "amount": 500_000,
    }
    # 2턴 뒤 받는 분은 질문 파일에 없으니 사람이 본다(고정 세트 S14의 PHP 20,000과 비교하지 않음).
    assert s14["remitCheck"]["outcome"] == "correct"
    assert s14["remitCheck"]["guessErrors"] == []
    assert s14["remitCheck"]["reason"] == "2턴 값(recipient)은 사람 확인"
    assert rows["R03"]["remitCheck"]["outcome"] == "declined"
    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert f"질문 파일 {path.resolve()} (문항 3개, 레포 밖 파일)" in summary

    # 다시 채점은 외부 파일을 다시 읽지 않는다.
    path.unlink()
    rescored = tmp_path / "rescored.jsonl"
    assert EVAL.main(["--rescore", str(output), "--output", str(rescored)]) == 0
    again = json.loads(rescored.read_text(encoding="utf-8").splitlines()[0])
    assert "expectedToolsFrom" not in again
    assert _rows(rescored)["S14"]["remitCheck"]["outcome"] == "correct"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expectedAction": "ASK_AMOUNT"}, "R01: category/expectedAction mismatch"),
        ({"expectedCurrency": "USD"}, "R01: bad expectedCurrency"),
        ({"followUpReply": "to mama"}, "R01: followUpReply only (and always)"),
        ({"note": None, "extra": 1}, "R01: fields must be"),
    ],
)
def test_external_question_file_is_validated_before_any_call(
    tmp_path: Path, change: dict[str, Any], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    questions = _external_questions()
    questions[0] = {**questions[0], **change}
    path = _external_file(tmp_path, questions)
    with pytest.raises(SystemExit):
        EVAL.main(
            ["--remit-questions", str(path), "--output", str(tmp_path / "x.jsonl")],
            settings=_settings(),
        )
    assert message in capsys.readouterr().err


def test_fixed_set_passes_the_same_validation() -> None:
    assert EVAL.remit_spec_errors(EVAL.load_questions(EVAL.REMIT_QUESTIONS)) == []


# --- 환율 단위 판정(rateUnit) -------------------------------------------------------------


def _rate_row(answer: str) -> dict[str, Any]:
    row = _row("S01", {"answer": answer, **_quote(11, 500_000, "KRW")})
    row["answer"] = answer
    return row


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        # 사람이 correct로 본 문장(측정 답변에서 옮김)
        ("Lorna will receive 10,000 PHP at the applied rate of 22.32 KRW per PHP.", "correct"),
        ("You would send 446,400 KRW at a rate of 22.32 won per peso.", "correct"),
        ("You would send 334,800 KRW at an applied rate of 22.32 KRW per 1 PHP.", "correct"),
        ("The rate is 1 PHP = 22.32 KRW.", "correct"),
        ("More precisely, 1 PHP costs 22.32 KRW.", "correct"),
        ("One peso is 22.32 won.", "correct"),
        # 사람이 wrong으로 본 문장
        ("She would receive 13,440 PHP at a rate of 22.32 PHP per 1,000 KRW.", "wrong"),
        ("The applied rate is 22.32 PHP per 1 KRW unit in the quote shown.", "wrong"),
        ("At 22.32 pesos per won.", "wrong"),
        ("The rate is 22.32 per KRW.", "wrong"),
        ("1 KRW = 22.32 PHP.", "wrong"),
        # 한 답변에 correct와 wrong이 섞임(S10 1회차)
        ("It uses 22.32 PHP per 1,000 KRW... more precisely, 1 PHP costs 22.32 KRW.", "wrong"),
        # 단위 없이 숫자만(S27 1회차)
        ("She would receive 22,401 PHP at the applied rate of 22.32.", "no_unit"),
        # 판단 불가
        ("The applied rate is 22.32 KRW today.", "unclear"),
    ],
)
def test_rate_unit_verdict(answer: str, verdict: str) -> None:
    check = EVAL.rate_unit_check(_rate_row(answer), answer)
    assert check is not None
    assert check["verdict"] == verdict, check["mentions"]


def test_no_rate_mention_has_no_verdict() -> None:
    assert (
        EVAL.rate_unit_check(_rate_row("Please check the card."), "Please check the card.") is None
    )
    # 다른 숫자 22.3이나 222.32는 환율 언급이 아니다.
    text = "You send 22.3% and 222.32 KRW."
    assert EVAL.rate_unit_check(_rate_row(text), text) is None


def test_unclear_rate_unit_goes_to_a_person_and_into_the_summary() -> None:
    answers = {
        "S01": "Lorna will receive 22,401 PHP. The applied rate is 22.32 KRW today.",
        "S04": "Lorna will receive 10,000 PHP at a rate of 22.32 PHP per 1,000 KRW.",
    }
    rows = []
    for qid, answer in answers.items():
        row = _row(qid, {**_quote(11, 500_000, "KRW"), "answer": answer})
        row.update(
            variant="v", repeat=1, warmup=False, toolResult=EVAL.fixture_context(),
            contextTexts=[_questions()[qid]["text"]], expectedTools=[], judgmentFacts=False,
            timings={}, serverTimings={}, usage=None, metrics=None, costUsd=0,
        )  # fmt: skip
        EVAL.score(row)
        rows.append(row)
    assert rows[0]["rateUnit"]["verdict"] == "unclear"
    assert rows[0]["remitCheck"]["needsReview"] is True
    assert rows[0]["remitCheck"]["reason"].endswith("환율 단위 확인")
    assert rows[1]["rateUnit"]["verdict"] == "wrong"
    conditions = {
        "target": "ai", "mode": "fake", "questions": [], "repeats": 1, "warmup": 0,
        "maxUsd": 1, "startedAt": "t", "status": "completed",
        "variants": [{"name": "v", "model": "m"}],
    }  # fmt: skip
    summary = EVAL.render_summary(conditions, rows)
    assert (
        "- 환율 단위: 환율을 말한 답변 2/2, correct 0, wrong 1 (S04#1), no_unit 0, unclear 1"
        in summary
    )
    assert "| 환율 단위 | 사람 확인 |" in summary


# --- 환율 표기 비교 측정 설정(이슈 #45) --------------------------------------------------


def test_rate_text_variants_differ_only_in_prompt_version() -> None:
    specs = Path(EVAL.AI_ROOT) / "specs" / "chat"
    base = EVAL.load_variants(specs / "variants_remit_v1.json", _settings())[0]
    loaded = EVAL.load_variants(specs / "variants_rate_text_v1.json", _settings())
    assert [(v["name"], v["promptVersion"]) for v in loaded] == [
        ("rate-v3", "v3"),
        ("rate-v3.1", "v3.1"),
        ("rate-v3.2", "v3.2"),
    ]
    same = {k: v for k, v in base.items() if k not in ("name", "promptVersion")}
    for variant in loaded:
        assert {k: v for k, v in variant.items() if k not in ("name", "promptVersion")} == same


def test_rate_text_variants_run_in_fake_mode(tmp_path: Path) -> None:
    settings = _settings()
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_rate_text_v1.json"
    output = tmp_path / "rate.jsonl"
    code = EVAL.main(
        ["--base-url", "http://testserver", "--question-set", "remit", "--questions", "S01,S14",
         "--variants", str(variants), "--output", str(output)],
        client=_client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )  # fmt: skip
    assert code == 0
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in lines if r["type"] == "call"]
    assert sorted({r["variant"] for r in rows}) == ["rate-v3", "rate-v3.1", "rate-v3.2"]
    assert all(r["remitCheck"]["outcome"] == "correct" for r in rows)
    assert {r["metrics"]["config"]["promptVersion"] for r in rows} == {
        "chat-v3",
        "chat-v3.1",
        "chat-v3.2",
    }
