import math


def zscore(x, mean, sd):
    """How many standard deviations x is from the mean."""
    if not sd:
        return math.nan
    return (x - mean) / sd


def clip(x, lo, hi):
    return max(lo, min(hi, x))
