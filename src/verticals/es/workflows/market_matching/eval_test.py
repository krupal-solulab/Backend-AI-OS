"""Definition-of-Done marker (docs/WORKFLOW_TEMPLATE.md calls for an
``eval_test.py`` inside each workflow package).

The actual pytest-discovered eval test lives at
``tests/test_es_market_matching.py`` — this project's ``pyproject.toml`` sets
``testpaths = ["tests"]`` (see the existing MGA smoke tests, which live there
too, not under ``src/verticals/mga/...``), so a test file here would silently
never run. This is a deliberate, documented departure from the template's
literal file location to match the project's REAL pytest configuration
rather than break test discovery. See tests/test_es_market_matching.py for
the eval against the real Workflow_10 dataset (all 6 submissions, expected
rankings/exclusions/zero-match, plus the missing-ACORD REQUEST_INFO case).
"""
