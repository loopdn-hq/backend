"""Ensures the Celery app is loaded whenever Django starts.

Without this, @shared_task has nothing to bind to and tasks silently fail to
register.
"""
from loopd.celery import app as celery_app

__all__ = ('celery_app',)
