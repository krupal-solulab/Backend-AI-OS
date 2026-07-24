"""The real acceptance test for this workflow lives at
``tests/test_es_agent_communication.py``, not here — this project's
``pyproject.toml`` sets ``testpaths = ["tests"]``, so pytest never discovers
tests under ``src/verticals/...``. See that file for the Definition of Done
against all 6 ``trigger_XX`` fixtures in ``TEST_DATA_ROOT/Workflow_12``.
"""
