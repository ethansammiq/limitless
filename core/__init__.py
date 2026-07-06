"""Core primitives shared by the settlement-source jobs.

obs.py (station-day observations + settlement-certainty bounds),
brackets.py (bracket subtitle parsing / deadness), fees.py (Kalshi taker
fee), io.py (atomic writes). The broker/opportunity/LLM-synth protocols
died with the KDE stack (2026-07-06).
"""
