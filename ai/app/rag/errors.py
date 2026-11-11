"""RAG 파이프라인이 공유하는 오류 타입."""


class RagError(Exception):
    """파이프라인 오류의 기반 클래스."""


class ContractBlockedError(RagError):
    """외부 계약(MySQL, Spring, R2, 금융 스키마, 제공자 API)이 확정되지 않았다.

    blocker를 해소할 수 있도록 어떤 정보가 빠졌는지 메시지에 정확히 적어야 한다.
    """


class ValidationFailedError(RagError):
    """입력 또는 저장된 데이터가 확정된 계약을 위반했다."""


class VersionConflictError(RagError):
    """같은 문서 버전인데 원본 해시나 청크 내용이 다르다."""


class ExtractionError(RagError):
    """소스 파일을 추출하지 못했다."""
