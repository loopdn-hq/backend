"""The admin site's API: admin.loopdn.com's backend, at /api/v1/admin/.

Loopd'n's own tools, the way the `commercial` app is the commercial site's.
Every route needs `platform.access` - the admin site's door, which the `admin`
role holds - and then the feature for its own area
(`platform.campaign.review`, ...). See frontend-web/docs/admin-site.md.

It owns no models. Each area is a thin API over the app that owns the data
and its rules - campaign review calls commercial/services/transitions.py and
commercial/services/review.py - so a rule is never written twice, once for
companies and once for admins.

Growing it is one area at a time, the same three files each:

    viewsets/v1/<area>.py      the routes, registered in urls.py
    serializers/<area>.py      the shapes
    permissions.py             the area's feature gate

These are the only routes that are not scoped to the caller's own objects:
an admin's queryset is everybody's, which is why every one sits behind two
features rather than one.
"""
