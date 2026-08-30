"""
Liveness endpoint for the load balancer.

Every route under /api/v1/ requires authentication (DEFAULT_PERMISSION_CLASSES
is IsAuthenticated), so without this there is no URL that returns an
unauthenticated 200 - the ALB health check would read every 401 as a dead
container, kill the task, and start another one that fails the same way.

Deliberately shallow: it reports that the process is up and serving, and checks
nothing else. A health check that also pinged Postgres and Redis would turn a
few seconds of database trouble into ECS terminating every healthy task and
being unable to replace them, which is strictly worse than serving errors while
the dependency recovers. Dependency health belongs in monitoring, not in the
check that decides whether to kill the container.
"""
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({'status': 'ok'}, status=200)
