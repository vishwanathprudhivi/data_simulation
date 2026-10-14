"""Scenario compiler and (from M10) the conversational analyst.

The LLM compiles requests into specs and narrates results; it never computes a
number. All model access goes through :class:`simcore.core.policy.reasoning.ReasoningClient`.
"""
