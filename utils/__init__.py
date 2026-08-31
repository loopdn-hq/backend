"""Cross-cutting helpers that belong to no single app.

Anything here must stay importable from every app without creating a cycle, so
these modules import from `django.conf` and the standard library only - never
from `user`, `otp` or `chat`.
"""
