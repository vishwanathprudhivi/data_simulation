"""Semantic layer (FR-SEM-*, FR-VIZ-*): governed queries over run outputs and chart specs."""

from services.semantic.charts import ChartValidationError, render_chart, validate_chart
from services.semantic.query import QueryError, QueryResult, SemanticEngine

__all__ = [
    "ChartValidationError",
    "QueryError",
    "QueryResult",
    "SemanticEngine",
    "render_chart",
    "validate_chart",
]
