"""Bounded allocation and lazy interleaving of platform-owned length generators."""

from collections import deque


def allocations(minimum, maximum, quantity, *, lower, upper, capacity):
    if type(minimum) is not int or type(maximum) is not int or not lower <= minimum <= maximum <= upper:
        raise ValueError(f"اختر نطاق طول صحيحًا من {lower} إلى {upper}؛ الحد الأدنى لا يتجاوز الأعلى.")
    lengths = range(minimum, maximum + 1)
    limits = {length: capacity(length) for length in lengths}
    if type(quantity) is not int or not 1 <= quantity <= sum(limits.values()):
        raise ValueError("الكمية يجب أن تكون موجبة وألا تتجاوز المساحة الفريدة للنطاق.")
    counts = dict.fromkeys(lengths, 0)
    remaining = quantity
    active = list(lengths)
    while remaining:
        share, extra = divmod(remaining, len(active))
        exhausted = [
            length for index, length in enumerate(active) if limits[length] < share + (index < extra)
        ]
        if exhausted:
            for length in exhausted:
                counts[length] = limits[length]
                remaining -= limits[length]
                active.remove(length)
            continue
        for index, length in enumerate(active):
            counts[length] = share + (index < extra)
        break
    return {length: count for length, count in counts.items() if count}


def interleave(streams):
    """Keep only one iterator per represented length, including unequal allocations."""
    pending = deque(map(iter, streams))
    while pending:
        stream = pending.popleft()
        try:
            value = next(stream)
        except StopIteration:
            continue
        yield value
        pending.append(stream)
