"""Work that belongs after the commit, on a worker when there is one.

Three things have to be true of anything a message write path hands off, and
each of them has been got wrong here at least once:

    after the commit    the row has to be readable by whoever picks the work
                        up, and a provider call inside an open transaction
                        holds a database connection across a network round trip
    on a worker         when a broker is configured
    inline otherwise    a laptop and a test run have no worker, and work that
                        silently never happens is worse than work that happens
                        on the request thread
    and never raising   this runs from `transaction.on_commit`, which Django
                        executes on the request thread AFTER the commit, so an
                        exception becomes a 500 on a message that is already
                        saved, already broadcast and already on everyone's
                        screen

`mail/outbound.py` and `sms/outbound.py` each grew their own copy of this
before it was a shared thing; theirs are load-bearing and their tests patch
their private names, so they are left alone rather than churned. Anything NEW
uses this one.
"""
import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def after_commit(label, queue, run_here):
    """Do this once the current transaction commits, wherever it can run.

    `label` names the work in the log - it is what somebody reads at 2am.
    `queue` hands it to Celery (typically `task.delay(id)`), `run_here` does it
    in this process. Both are callables taking no arguments, so the caller
    keeps hold of its own ids and this stays ignorant of what the work is.
    """
    transaction.on_commit(lambda: run_somewhere(label, queue, run_here))


def run_somewhere(label, queue, run_here):
    """The same decision, for a caller that is already past the commit.

    The activity feed's listeners are the case: `notification/services.py`
    already defers them to on_commit, so asking for a second on_commit there
    would run them immediately anyway and only obscure that.
    """
    from loopd.celery import worker_available

    try:
        if worker_available():
            try:
                queue()
                return
            except Exception:
                # A broker that is configured but unreachable must not swallow
                # the work. Running it here is strictly better than losing it.
                logger.exception('Could not queue %s; running inline', label)

        run_here()
    except Exception:
        logger.exception('%s failed', label)
