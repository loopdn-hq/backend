"""The ads app's models, one file per model, like user/models/.

Everything is re-exported here, so `from ads.models import Campaign` works.
docs/advertising_integration_plan.md is the design: read its Naming section
before adding a model, because several obvious names here already mean
something else in this codebase ("campaign", "account", "channel", "placement").

Nothing reads or writes these tables yet. They are Stage 1a of the plan, and
the running app behaves identically with them present and empty.
"""
from .ad_click import AdClick
from .ad_conversion import AdConversion
from .ad_impression import AdImpression
from .ad_settings import AdSettings
from .ad_transaction import AdTransaction
from .advertiser import Advertiser
from .campaign import Campaign
from .campaign_eligibility import CampaignEligibility
from .creative import Creative, creative_image_upload_to
from .targeting_criteria import TargetingCriteria

__all__ = [
    'AdClick',
    'AdConversion',
    'AdImpression',
    'AdSettings',
    'AdTransaction',
    'Advertiser',
    'Campaign',
    'CampaignEligibility',
    'Creative',
    'TargetingCriteria',
    'creative_image_upload_to',
]
