"""JSON the browser can always parse.

Python writes float('inf') as `Infinity` and NaN as `NaN`; JSON.parse rejects both, which froze the
dashboard as soon as every closed trade was a win (profit factor = inf). Non-finite numbers become
the strings "Infinity" / "-Infinity" (JS isFinite() and Number() still understand them) and NaN null.
"""
from __future__ import annotations

import json
import math
from collections import deque


def clean(o):
    if isinstance(o, float):
        if math.isnan(o):
            return None
        if math.isinf(o):
            return "Infinity" if o > 0 else "-Infinity"
        return o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque, set, frozenset)):
        return [clean(v) for v in o]
    return o


def dumps(o, **kw) -> str:
    try:
        return json.dumps(o, default=str, allow_nan=False, **kw)    # fast path: nothing to fix
    except ValueError:
        return json.dumps(clean(o), default=str, **kw)
