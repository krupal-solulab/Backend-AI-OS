"""Definition-of-Done marker (docs/WORKFLOW_TEMPLATE.md calls for an
``eval_test.py`` inside each workflow package).

The actual pytest-discovered eval test lives at
``tests/test_es_package_assembly.py`` — this project's ``pyproject.toml``
sets ``testpaths = ["tests"]`` (same precedent as market_matching's
eval_test.py), so a test file here would never actually run. See
tests/test_es_package_assembly.py for the eval against all 6 real
Workflow_11 scenarios, including Scenario 04 as a mandatory, non-skippable
release-gate case per the PRD's Section 9 risk register.
"""
