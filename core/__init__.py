"""Core trading primitives — broker abstractions, opportunity contract, LLM synth.

Domain-agnostic plumbing shared across all market scanners (weather, policy,
legal, FDA, entertainment). Downstream pipeline (auto_trader, execute_trade,
position_monitor, trading_guards) depends only on these protocols.
"""
