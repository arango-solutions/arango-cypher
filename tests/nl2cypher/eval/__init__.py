"""Eval harness entry points (corpus, runner, configs, gate).

Design rationale: a repeatable measurement of pipeline accuracy and cost
makes the four WP-25 features (few-shot, entity resolution,
execution-grounded validation, prompt caching) testable as a system
rather than in isolation.  The gate then prevents silent regressions.
"""
