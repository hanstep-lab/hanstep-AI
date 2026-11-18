"""금융 정책 스펙 가드 ②(#88): 항 단위 라벨.

- 라벨 표(specs/rag/finance_policy_labels_v1.json)는 적재 범위 조문만 가리키고
  값은 정해진 범위 안이다.
- 조각이 걸친 항을 모두 찾는다(긴 조문을 나눈 조각, 짧은 단위를 합친 조각).
- 켜면 결과마다 발행처와 걸친 항의 라벨(direction·appliesTo·note)이 붙는다.
- 끄면(기본) 도구 결과가 v4.2 after와 바이트 단위로 같다.

실제 문서·모델 없음(가짜 검색기, 합성 조문).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.chat.finance_policy_tools import SEARCH_FINANCE_POLICY, FinancePolicyTool
from app.core.config import Settings
from app.finance_policy.chunks import PolicyChunk
from app.finance_policy.corpus import Unit, load_manifest
from app.finance_policy.labels import (
    DIRECTIONS,
    LABELS_PATH,
    PolicyLabels,
    chunk_paragraphs,
    default_labels,
)
from app.finance_policy.search import PolicyHit

pytestmark = pytest.mark.unit

SAMPLE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "finance_policy_result_sample.json"
)


class FakeSearcher:
    def __init__(self, hits: list[PolicyHit]) -> None:
        self.hits = hits

    def search(self, query: str, k: int) -> tuple[list[PolicyHit], str]:
        return self.hits[:k], "hybrid"


def _run(tool: FinancePolicyTool) -> Any:
    return asyncio.run(tool.run(SEARCH_FINANCE_POLICY, {"queryKo": "외국인거주자 송금 한도"}))


def test_label_table_points_at_loaded_articles_only() -> None:
    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    documents = {d["key"]: d for d in load_manifest()["documents"]}
    assert set(data["issuers"]) == set(documents)
    assert data["issuers"]["GUIDE"] == {
        "issuer": "금융감독원",
        "basis": "가이드북 판권면(사용자 확인)",
    }
    for item in data["labels"]:
        assert item["direction"] in DIRECTIONS, item
        assert item["article"] in documents[item["doc"]]["articles"], item
        assert item["appliesTo"] and item["summary"], item
    # 사용자 확정(2026-11-18): ④는 수령, ① 3호 단서는 send + note.
    by_ref = {(i["article"], i["paragraph"], i.get("scope")): i for i in data["labels"]}
    assert by_ref[("제4-4조", "④", None)]["direction"] == "receive"
    proviso = by_ref[("제4-4조", "①", "3호 단서")]
    assert proviso["direction"] == "send"
    assert proviso["note"] == "문언은 '수령'이지만 지급 조항(①)의 단서로 해석"
    assert by_ref[("제4-3조", "③", None)]["appliesTo"].startswith("거주자(외국인거주자 제외)")
    # 전 항 삭제 조문은 라벨이 없다.
    assert not {"제4-6조", "제4-7조"} & {i["article"] for i in data["labels"] if i["doc"] == "REG"}


ARTICLE = "\n".join(
    [
        "제4-4조(비거주자또는외국인거주자의지급등) ① 취득경위입증서류를 제출하여 지급할수있다.",
        "3.국내보수 범위에서 지정거래외국환은행을통해지급하는경우.",
        "② 연간미화5만불범위내에서 지급할수있다.",
        "③ 다음 각호의금액을지급할수있다.",
        "④ 다음각호의1에해당하는경우 증빙서류를제출하지아니하고수령할수있다.",
        "1.건당5천불이내인경우",
    ]
)


def test_split_pieces_get_every_paragraph_they_cross() -> None:
    unit = Unit("REG", "REG:제4-4조", "비거주자또는외국인거주자의지급등", ARTICLE)
    lines = ARTICLE.splitlines()
    pieces = ["\n".join(lines[:2]), "\n".join(lines[2:4]), "\n".join(lines[4:])]
    chunks = [
        PolicyChunk(f"unit:REG:제4-4조#{i}", "REG", text, ("REG:제4-4조",), 28, "2026-11-02")
        for i, text in enumerate(pieces)
    ]
    spans = chunk_paragraphs(chunks, [unit])
    # 조 제목(항 표시 앞, '')과 제목 바로 뒤의 ①
    assert [m for _, m in spans["unit:REG:제4-4조#0"]] == ["", "①"]
    assert [m for _, m in spans["unit:REG:제4-4조#1"]] == ["②", "③"]
    assert [m for _, m in spans["unit:REG:제4-4조#2"]] == ["④"]
    # 항 중간에서 시작하는 조각은 이어지는 앞 항부터 센다.
    middle = PolicyChunk("unit:REG:제4-4조#9", "REG", lines[1], ("REG:제4-4조",), 28, None)
    assert [m for _, m in chunk_paragraphs([middle], [unit])["unit:REG:제4-4조#9"]] == ["①"]


def test_merged_short_units_keep_each_unit() -> None:
    short = Unit("EFTA", "EFTA:제2조", "정의", "제2조(정의) 10. “접근매체”라 함은 …")
    law = Unit(
        "EFTA", "EFTA:제6조", "접근매체", "제6조(접근매체)\n  ③ 누구든지 양도하여서는 아니 된다."
    )
    merged = PolicyChunk(
        "unit:EFTA:제2조#0",
        "EFTA",
        short.text + "\n" + law.text,
        ("EFTA:제2조", "EFTA:제6조"),
        None,
        None,
    )
    spans = chunk_paragraphs([merged], [short, law])["unit:EFTA:제2조#0"]
    assert spans == (("EFTA:제2조", ""), ("EFTA:제6조", ""), ("EFTA:제6조", "③"))
    out = default_labels().for_spans("EFTA", spans)
    assert out["issuer"] == "법률"
    assert [label["ref"] for label in out["labels"]] == ["제2조 10호", "제6조 ③ 1~5호"]


def _hit(paragraphs: tuple[tuple[str, str], ...]) -> PolicyHit:
    return PolicyHit(
        "외국환거래규정",
        "재정경제부고시 제2026-110호",
        "제4-4조",
        "2026-11-02",
        "ADMIN_RULE",
        ARTICLE,
        "unit:REG:제4-4조#1",
        doc="REG",
        paragraphs=paragraphs,
    )


def test_labels_on_add_issuer_and_paragraph_labels() -> None:
    spans = (("REG:제4-4조", "①"), ("REG:제4-4조", "④"))
    on = _run(FinancePolicyTool(FakeSearcher([_hit(spans)]), timeout=1, labels=default_labels()))
    result = json.loads(on.content)["results"][0]
    # 원래 필드는 그대로이고 뒤에 issuer·labels만 붙는다.
    assert list(result)[:5] == ["document", "ref", "quote", "number", "effectiveFrom"]
    assert result["issuer"] == "재정경제부"
    labels = {label["ref"]: label for label in result["labels"]}
    assert labels["제4-4조 ④"]["direction"] == "receive"
    assert labels["제4-4조 ④"]["appliesTo"] == "비거주자·외국인거주자"
    assert labels["제4-4조 ① 3호 단서"]["note"] == "문언은 '수령'이지만 지급 조항(①)의 단서로 해석"
    assert {label["direction"] for label in result["labels"]} == {"send", "receive"}
    guide = PolicyHit(
        "가이드북", None, "p.109", "2022-07", "GUIDE", "…", "unit:GUIDE:p109:9#0", "GUIDE"
    )
    out = default_labels().for_spans("GUIDE", guide.paragraphs)
    assert out == {"issuer": "금융감독원"}  # 가이드북은 항 라벨 없이 발행처만


def test_labels_off_is_byte_identical_to_v4_2_after() -> None:
    # 기준: v4.2 after와 같은 코드로 만든 검색 결과 예시(A01 검색어, ③, k=8).
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    hits = []
    for i, r in enumerate(sample["result"]["results"]):
        law = "effectiveFrom" in r
        hits.append(
            PolicyHit(
                r["document"],
                r.get("number"),
                r["ref"],
                r["effectiveFrom"] if law else r["issuedOn"],
                "ADMIN_RULE" if law else "GUIDE",
                r["quote"],
                f"chunk{i}",
                doc="REG" if law else "GUIDE",
                paragraphs=(("REG:제4-4조", "④"),) if law else (),
            )
        )
    expected = json.dumps(sample["result"], ensure_ascii=False)
    off = _run(FinancePolicyTool(FakeSearcher(hits), timeout=1))
    assert off.content == expected
    off_explicit = _run(FinancePolicyTool(FakeSearcher(hits), timeout=1, labels=None))
    assert off_explicit.content == expected
    on = _run(FinancePolicyTool(FakeSearcher(hits), timeout=1, labels=default_labels()))
    assert on.content != expected


def test_setting_defaults_off() -> None:
    assert Settings(_env_file=None).finance_policy_labels is False  # type: ignore[call-arg]
    assert PolicyLabels.load().issuers["PRESS"] == "금융감독원"
