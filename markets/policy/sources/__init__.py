"""Source adapters for policy markets.

Each adapter knows how to: (a) map a Kalshi ticker to its primary source
URL(s), (b) fetch the current state of that source, (c) detect whether it's
been updated within the configured freshness window. Adapters return
DocBundle objects that the scanner feeds to core/llm_synth.py.
"""
