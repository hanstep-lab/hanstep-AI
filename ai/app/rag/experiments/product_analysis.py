"""금융상품 검색 ① 실패 분석과 말뭉치 점검(임베딩 없이 ① 보고서와 청크로만 계산).

- 실패 문항: 정답 문서가 1위가 아니거나 근거 적중을 놓친 문항. 분류(A~E, 불명)는 사람이 청크를
  읽고 붙인 라벨 파일에서 가져온다. 여기서는 판단하지 않는다.
- 추출 점검: 페이지당 글자 수, 짧은 페이지, 깨진 문자, 한글 없는 페이지.
- 공통 문구 점검: 서로 다른 문서의 거의 같은 청크 쌍(기준은 ``NEAR_DUPLICATE_CRITERIA``).
- 출처·보안 점검: 파일·해시·쪽수·사본 여부, 번호·전화·URL 패턴 수, 지시문처럼 보이는 문장 수.
  운영 적재 검사가 아니라 현재 말뭉치 상태 기록이다. 필터나 수정은 하지 않는다.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from app.rag.experiments.product_corpus import CorpusError, ProductDocument, match_key
from app.rag.experiments.product_eval import CorpusChunk

LABELS_PATH = Path(__file__).with_name("product_failure_labels_v1.json")
CATEGORIES = ("A", "B", "C", "D", "E", "불명")
CATEGORY_NAMES = {
    "A": "추출",
    "B": "공통 문구",
    "C": "질문 모호",
    "D": "표현 차이",
    "E": "약관 혼동",
    "불명": "불명",
}
EXCERPT_CHARS = 200
SHORT_PAGE_CHARS = 200
PREFIX_CHARS = 100
SENTENCE_OVERLAP = 0.8
NEAR_DUPLICATE_CRITERIA = (
    f"서로 다른 문서의 청크 쌍 중 (1) 공백을 모두 지운 앞 {PREFIX_CHARS}자가 같거나 "
    f"(2) 문장 집합의 Jaccard(교집합/합집합)가 {SENTENCE_OVERLAP} 이상. "
    "문장은 줄바꿈과 '. ? !' 뒤 공백에서 나누고 공백을 지워 비교한다."
)

_HANGUL = re.compile(r"[가-힣]")
_SENTENCE_SPLIT = re.compile(r"\n+|(?<=[.?!])\s+")
PATTERNS = {
    "registration_13": re.compile(r"(?<!\d)\d{6}-\d{7}(?!\d)"),
    "digits_10_16": re.compile(r"(?<!\d)\d{10,16}(?!\d)"),
    "phone": re.compile(r"(?<!\d)(?:0\d{1,2}-\d{3,4}-\d{4}|1[5-9]\d{2}-\d{4})(?!\d)"),
    "url": re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE),
}
INSTRUCTION_WORDS = re.compile(
    r"ignore|instruction|system prompt|you are|무시하|지시|명령", re.IGNORECASE
)


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def failing_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """정답 문서가 1위가 아니거나 근거 적중을 놓친 문항(겹치면 한 번)."""
    return [r for r in rows if r["doc_rank"] != 1 or not r["evidence@k"]]


def load_labels(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = data.get("labels")
    if not isinstance(labels, dict):
        raise CorpusError(f"{path}: expected a 'labels' object")
    for qid, label in labels.items():
        if label.get("category") not in CATEGORIES or not label.get("reason"):
            raise CorpusError(f"{path}: label {qid} needs category in {CATEGORIES} and reason")
    return dict(data)


def failure_cases(
    rows: Sequence[Mapping[str, Any]],
    chunks: Sequence[CorpusChunk],
    gold_quotes: Mapping[str, str],
    labels: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """실패 문항마다 1위 청크와 정답 근거 청크의 앞부분, 사람이 붙인 분류를 모은다."""
    by_id = {c.chunk_id: c for c in chunks}
    cases: list[dict[str, Any]] = []
    for row in failing_rows(rows):
        quote = match_key(gold_quotes[row["id"]])
        evidence = next(
            (c for c in chunks if c.doc_id == row["gold_doc_id"] and quote in match_key(c.text)),
            None,
        )
        top = by_id[row["hits"][0]["chunk_id"]]
        label = labels.get(row["id"], {"category": "불명", "reason": "라벨 없음"})
        cases.append(
            {
                "id": row["id"],
                "question": row["question"],
                "names_product": row["names_product"],
                "gold_document": row["gold_document"],
                "doc_rank": row["doc_rank"],
                "evidence": bool(row["evidence@k"]),
                "top1_document": row["hits"][0]["document"],
                "top1_chunk_id": top.chunk_id,
                "top1_excerpt": excerpt(top.text),
                "evidence_chunk_id": evidence.chunk_id if evidence else None,
                "evidence_excerpt": excerpt(evidence.text) if evidence else None,
                "category": label["category"],
                "reason": label["reason"],
            }
        )
    return cases


def extraction_check(documents: Sequence[ProductDocument]) -> list[dict[str, Any]]:
    """문서별 페이지당 추출 글자 수와 깨짐 신호. 텍스트가 없는 페이지는 0자로 센다."""
    result: list[dict[str, Any]] = []
    for document in documents:
        pages: dict[int, list[str]] = {n: [] for n in range(1, (document.pages or 0) + 1)}
        for block in document.blocks:
            if block.page_number is not None:
                pages.setdefault(block.page_number, []).append(block.text)
        chars = {n: len("\n".join(texts)) for n, texts in pages.items()}
        counts = list(chars.values()) or [0]
        text = document.text
        result.append(
            {
                "doc_id": document.doc_id,
                "name": document.name,
                "pages": document.pages,
                "min_chars": min(counts),
                "median_chars": statistics.median(counts),
                "short_pages": [n for n, c in chars.items() if c < SHORT_PAGE_CHARS],
                "replacement_chars": text.count("�"),
                "cid_patterns": text.count("(cid:"),
                "no_hangul_pages": [
                    n for n, texts in pages.items() if not _HANGUL.search("".join(texts))
                ],
            }
        )
    return result


def near_duplicates(chunks: Sequence[CorpusChunk], examples: int = 3) -> dict[str, Any]:
    """서로 다른 문서의 거의 같은 청크 쌍(기준은 NEAR_DUPLICATE_CRITERIA)."""
    keys = {c.chunk_id: match_key(c.text) for c in chunks}
    sets = {c.chunk_id: {match_key(s) for s in sentences(c.text)} for c in chunks}
    pairs: list[dict[str, Any]] = []
    for a, b in combinations(chunks, 2):
        if a.doc_id == b.doc_id:
            continue
        same_prefix = keys[a.chunk_id][:PREFIX_CHARS] == keys[b.chunk_id][:PREFIX_CHARS]
        union = sets[a.chunk_id] | sets[b.chunk_id]
        jaccard = len(sets[a.chunk_id] & sets[b.chunk_id]) / len(union) if union else 0.0
        if same_prefix or jaccard >= SENTENCE_OVERLAP:
            pairs.append(
                {
                    "a": a.chunk_id,
                    "b": b.chunk_id,
                    "same_prefix": same_prefix,
                    "sentence_jaccard": round(jaccard, 3),
                    "excerpt": excerpt(a.text, 120),
                }
            )
    # 예시는 문장 겹침이 큰 쌍부터 보여 준다(앞 100자만 같은 쌍은 공통 머리말일 때가 많다).
    ranked = sorted(pairs, key=lambda p: (-p["sentence_jaccard"], p["a"], p["b"]))
    return {
        "criteria": NEAR_DUPLICATE_CRITERIA,
        "pairs": len(pairs),
        "same_prefix_pairs": sum(1 for p in pairs if p["same_prefix"]),
        "sentence_overlap_pairs": sum(
            1 for p in pairs if p["sentence_jaccard"] >= SENTENCE_OVERLAP
        ),
        "examples": ranked[:examples],
    }


def security_scan(documents: Sequence[ProductDocument], examples: int = 3) -> dict[str, Any]:
    """번호·전화·URL 패턴 수와 지시문처럼 보이는 문장 수(값은 남기지 않고 세기만 한다)."""
    per_document: list[dict[str, Any]] = []
    instruction_examples: list[dict[str, str]] = []
    instruction_total = 0
    for document in documents:
        text = document.text
        counts = {name: len(pattern.findall(text)) for name, pattern in PATTERNS.items()}
        flagged = [s for s in sentences(text) if INSTRUCTION_WORDS.search(s)]
        instruction_total += len(flagged)
        for sentence in flagged:
            match = INSTRUCTION_WORDS.search(sentence)
            if match and len(instruction_examples) < examples:
                instruction_examples.append(
                    {"doc": document.name, "word": match.group(0), "sentence": excerpt(sentence)}
                )
        per_document.append({"name": document.name, **counts, "instruction_like": len(flagged)})
    totals = {name: sum(d[name] for d in per_document) for name in PATTERNS}
    return {
        "patterns": {name: pattern.pattern for name, pattern in PATTERNS.items()},
        "instruction_words": INSTRUCTION_WORDS.pattern,
        "totals": {**totals, "instruction_like": instruction_total},
        "per_document": per_document,
        "instruction_examples": instruction_examples,
    }


def source_list(documents: Sequence[ProductDocument]) -> list[dict[str, Any]]:
    """파일 이름 / doc id / 쪽수 / 사본 여부(같은 바이트 파일이 둘 이상이면 사본)."""
    return [
        {
            "file": name,
            "doc_id": document.doc_id,
            "sha256_12": document.sha256[:12],
            "pages": document.pages,
            "copy_of": [other for other in document.files if other != name],
        }
        for document in documents
        for name in document.files
    ]


def category_counts(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {c: sum(1 for case in cases if case["category"] == c) for c in CATEGORIES}
