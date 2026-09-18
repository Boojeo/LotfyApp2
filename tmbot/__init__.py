"""Semi-automated trade management and analytical assistant for Capital.com.

The bot never opens a position.  You take the entry by hand; it adopts the
position (with your confirmation), then runs the exit plan: partial closes at
TP1/TP2, an automatic move to break-even, and an ATR chandelier trailing stop
that tightens or holds with the trend.
"""

__version__ = "0.1.0"
