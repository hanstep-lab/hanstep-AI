"""Error types shared by the RAG pipeline."""


class RagError(Exception):
    """Base class for pipeline errors."""


class ContractBlockedError(RagError):
    """An external contract (MySQL, Spring, R2, finance schema, provider API) is unconfirmed.

    The message must say exactly which information is missing so the blocker can be resolved.
    """


class ValidationFailedError(RagError):
    """Input or stored data violates the confirmed contract."""


class VersionConflictError(RagError):
    """Same document version with a different source hash or chunk content."""


class ExtractionError(RagError):
    """A source file could not be extracted."""
