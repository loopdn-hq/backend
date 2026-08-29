"""Celery application.

Nothing schedules work yet; this is the wiring so that `celery -A loopd`
resolves and the worker/beat containers have something to attach to. The
first real users will be sending the OTP email and processing uploaded
attachments off the request path.
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'loopd.settings')

app = Celery('loopd')

# Every CELERY_-prefixed Django setting becomes Celery configuration, so
# there is one place to configure and one place to look.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Picks up tasks.py in each installed app.
app.autodiscover_tasks()
