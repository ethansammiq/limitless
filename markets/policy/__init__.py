"""Policy & legislative prediction markets (Kalshi KXCONFIRM*, KXBILL*, KXVOTE*, KXNOM*).

Source of truth: api.congress.gov. The scanner pulls primary documents
(committee reports, roll-call votes, bill text, nomination packages) when
they've been updated in the last N days, synthesizes a calibrated probability
via Claude Opus 4.7, and opens a trade only when the LLM probability diverges
from the market's implied probability by more than the configured threshold.
"""
