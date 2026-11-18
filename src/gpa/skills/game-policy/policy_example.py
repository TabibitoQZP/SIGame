"""Replace this neutral runnable policy using visual observations."""


def step(frame, state):
    return "(wait())", (state or 0) + 1
