"""Celery tasks for calls.

Thin on purpose, exactly as `mail/tasks.py` and `sms/tasks.py` are: every task
here is a wrapper with no logic of its own, so the work stays in
`calls/services.py` where a plain test can call it directly. Nothing in the test
suite touches Celery.
"""
from celery import shared_task


@shared_task(
    name='calls.close_room',
    # Closing a room that is already closed is a no-op, so a redelivery after a
    # worker dies costs nothing.
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def close_room_task(room_name):
    from calls import livekit

    # Raised rather than swallowed, unlike services.close_room: a worker has the
    # decorator above to retry it, where an inline call has nobody.
    livekit.delete_room(room_name)


@shared_task(name='calls.sweep_stale_calls')
def sweep_stale_calls():
    from calls.services import sweep_stale_calls as run

    return run()


@shared_task(name='calls.purge_expired_recordings')
def purge_expired_recordings():
    from calls.services import purge_expired_recordings as run

    return run()
