"""AI Assistant — a single chat surface grounded in the real, live data every
workflow already writes to (``ReviewItem``/``OutputPackage``/``Submission``),
reusing the same citation-enforced ``LLMService`` every workflow's drafting
step uses. No workflow-specific code lives here; it's pure cross-workflow
retrieval + the shared LLM contract.
"""
