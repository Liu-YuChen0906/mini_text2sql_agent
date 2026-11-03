"""Execution control errors must not be mistaken for SQL quality or repair errors."""


class ExecutionBudgetExceeded(RuntimeError):
    """Stop the current analysis with a durable partial/failed outcome."""
