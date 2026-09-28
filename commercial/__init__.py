"""What a commercial user does: their companies, campaigns, money and results.

"Commercial" is the user type that buys ads (the `commercial` account role, the
commercial.loopdn.com site). This app holds what that user does and nothing
else. The product plan also uses "commercial" for all of monetization (§10,
§24) - that is not this app: the Business plan, wallets and Communication
Credits stay in `billing`, custom pricing in `enterprise`.

Names, from docs/commercial_rename_plan.md: *commercial* is who, *company* is
what they manage (the plan's "advertiser"), *ads* is what they do - so the
permission keys stay `ads.*` and the models stay `Ad...`. No URL, path or upload
key here may contain "ads" or "advert": ad blockers drop them.
"""
