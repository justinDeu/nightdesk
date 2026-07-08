"""nightdesk in-pod runner (k8s execution target).

The ``nightdesk-runner`` console script (``main:main``) is the pod entrypoint.
This package is DB-less and FastAPI-less so the runner image stays slim; it
imports only the pure host pieces it reuses (backends, domain.diff, headless
prompt) plus stdlib for HTTP write-back.
"""
