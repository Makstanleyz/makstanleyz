"""
signal_engine.py — Validated signal container
"""
from dataclasses import dataclass, field
from typing import Optional
from core.scanner import Signal


@dataclass
class ValidatedSignal:
    signal:       Signal
    leverage:     int
    htf_aligned:  Optional[bool]   # True=aligned, False=counter, None=neutral (5m context)
    btc_confirms: Optional[bool]   # True=confirms, False=against, None=neutral
    fund_rate:    float
    score_mod:    int
    action:       str
    reason:       str
