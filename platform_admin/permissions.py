"""The admin site's feature gates, one per area.

`CanUseAdmin` goes on every route: the door to the admin site. Each area adds
its own, so an admin can be given some tools and not others. Both answer 403
with the feature's own sentence; neither is the access control on an object.
"""
from rbac.permissions import feature_required

#: The door: every admin route. Held by the `admin` role.
CanUseAdmin = feature_required('platform.access')

#: Approving and rejecting submitted campaigns (slice 2). Lockable to admin.
CanReviewCampaigns = feature_required('platform.campaign.review')
