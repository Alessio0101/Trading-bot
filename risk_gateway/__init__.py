"""
Independent risk gateway.

The only component allowed to turn a proposal into a (paper) order. It runs as
its own process, owns its limits and price source, and exposes no endpoint that
can change limits. Live trading is not implemented: the broker is paper-only.
"""
