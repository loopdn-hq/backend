"""The commercial app's models, one file per model, like user/models/.

Everything is re-exported here, so `from commercial.models import Campaign` works.
docs/advertising_integration_plan.md is the design: read its Naming section
before adding a model, because several obvious names here already mean
something else in this codebase ("campaign", "account", "channel", "placement").

docs/campaigns_final_plan.md is the build order. Slices 0, 1 and 3 have an
API: companies and which one is active, building and submitting a campaign,
and the money - adding funds, and setting a budget aside at submit. Slice 5
serves campaigns in chat and measures them: AdImpression, CampaignStatsDaily.
"""
from .ad_click import AdClick
from .ad_conversion import AdConversion
from .ad_impression import AdImpression
from .ad_settings import AdSettings
from .ad_transaction import AdTransaction
from .campaign import Campaign
from .campaign_event import CampaignEvent
from .campaign_eligibility import CampaignEligibility
from .campaign_stats_daily import CampaignStatsDaily
from .commercial_preference import CommercialPreference
from .company import Company, company_logo_upload_to
from .creative import Creative, creative_image_upload_to
from .industry import Industry
from .pricing_rate import PricingRate
from .targeting_criteria import TargetingCriteria

__all__ = [
    'AdClick',
    'AdConversion',
    'AdImpression',
    'AdSettings',
    'AdTransaction',
    'Campaign',
    'CampaignEligibility',
    'CampaignEvent',
    'CampaignStatsDaily',
    'CommercialPreference',
    'Company',
    'Creative',
    'Industry',
    'PricingRate',
    'TargetingCriteria',
    'company_logo_upload_to',
    'creative_image_upload_to',
]
