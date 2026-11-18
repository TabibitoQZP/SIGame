"""Minimal readable code policy: follow a red target in RGB.

This demonstrates the policy contract, not a solution to a benchmark task.
Copy to a coding workspace's policy/policy.py and submit.
"""

import numpy as np


def step(frame, state):
    rgb = frame.astype(np.int16)
    red = (
        (rgb[:, :, 0] > 160)
        & (rgb[:, :, 0] - rgb[:, :, 1] > 60)
        & (rgb[:, :, 0] - rgb[:, :, 2] > 60)
    )
    _, xs = np.nonzero(red)
    if not len(xs):
        return "(wait())", state
    action = "(press(right))" if float(xs.mean()) < frame.shape[1] / 2 else "(wait())"
    return action, {"calls": (state or {}).get("calls", 0) + 1}
