"""The commercial app's models, one file per model, like user/models/.

Everything is re-exported here, so `from commercial.models import Campaign` works.
docs/advertising_integration_plan.md is the design: read its Naming section
before adding a model, because several obvious names here already mean
something else in this codebase ("campaign", "account", "channel", "placement").

docs/campaigns_final_plan.md is the build order. Slice 0 (companies, and which
one is active) is the only part with an API; everything else is schema that
nothing reads or writes yet.
"""
from .ad_click import AdClick
from .ad_conversion import AdConversion
from .ad_impression import AdImpression
from .ad_settings import AdSettings
from .ad_transaction import AdTransaction
from .campaign import Campaign
from .campaign_eligibility import CampaignEligibility
from .commercial_preference import CommercialPreference
from .company import Company, company_logo_upload_to
from .creative import Creative, creative_image_upload_to
from .industry import Industry
from .targeting_criteria import TargetingCriteria

__all__ = [
    'AdClick',
    'AdConversion',
    'AdImpression',
    'AdSettings',
    'AdTransaction',
    'Campaign',
    'CampaignEligibility',
    'CommercialPreference',
    'Company',
    'Creative',
    'Industry',
    'TargetingCriteria',
    'company_logo_upload_to',
    'creative_image_upload_to',
]
