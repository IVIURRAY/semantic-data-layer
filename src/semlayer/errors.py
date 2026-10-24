class SemanticLayerError(Exception):
    """Base class for all data layer errors."""


class ModelError(SemanticLayerError):
    """The semantic model or physical mapping is inconsistent (raised at startup)."""


class QueryError(SemanticLayerError):
    """The query is invalid or would produce a meaningless number (raised per query)."""
