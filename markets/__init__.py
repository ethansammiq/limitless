"""Market-specific scanners that produce Opportunity instances.

Each subpackage corresponds to one market domain (policy, legal, FDA, etc.)
and owns: (a) a series-ticker whitelist, (b) one or more source adapters
under sources/, (c) a scanner that orchestrates filter -> fetch -> synth ->
divergence gate, (d) a trader CLI that pipes through the shared execution
pipeline.
"""
