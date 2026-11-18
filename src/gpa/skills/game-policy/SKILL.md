---
name: game-policy
description: Develop and iteratively test a real-time RGB game policy.
---

Read TASK.md and TOOLS.md. Write policy/policy.py implementing:
    def step(frame, state):
        return action, state

frame is a uint8 RGB numpy array. The initial state for every fresh submit is None.
Your state persists within that run, including across explicit in-run reloads.
Use claude_game_tools.context() to check epoch changes and maintain your own state correctly.

The game NEVER PAUSES during a submitted run. It continues while your policy or a visual service is busy.
The run lasts 60 real seconds. Input is scheduled on a fixed 200ms grid.
An action received 10ms after a boundary can start at the NEXT boundary, subject to FIFO queue wait.
At most one step is in flight. After completion the host provides the latest available tick observation.
A slow step skips intervening observations. Queue entries are not overwritten. Returning an action is not proof it executed.
context() includes source frame, frame age, times, queue, own execution events, epoch and remaining-time deadline.
Measure actual latency and adapt perception frequency, caching, prediction and controls yourself.

Return either a tick string, or a {"kind":...,"keys":[...],"click":[x,y]} object:
- "(press(right))", "(press(space,right))" (at most four keys)
- "(click(.3,.5))" (normalized [0,1] image coordinates, left click)
- "(press(space),click(.3,.5))"
- "(wait())"
- "(reset())" (fresh page; epoch increments, old queued results invalidated; 60s budget continues)

Keys: lowercase a-z, digits, up/down/left/right/space/enter/esc/shift/ctrl/alt/tab/backspace/delete.
Key releases are scheduled at the next tick independently of your code; actual device send/ack latency is recorded.
Consecutive keys are released and pressed again. Acknowledgements do not block scheduling the next tick's input.
Click presses/releases immediately in its tick. Unsupported actions raise errors.
Judge controls, instructions, progress, menus, death and restarts visually. No private score or success flag is provided.

You may use numpy, OpenCV and the documented nongenerative vision SDK at development and runtime:
    import claude_game_tools as tools
    tools.jev(frame, {"q":{"type":"choice","instructions":"...", "criteria":{"A":"...","B":"..."}}})
    tools.vision(frame, "ocr")
    tools.vision(frame, "sam", ...)
    tools.vision(frame, "depth")
    tools.context()

Jev answers fixed choices; it is not a free-form generative policy. Read vision_reference.md for concrete payload signatures.
All vision requests cost real latency. The host does not precompute or pause for them.
Vision result dictionaries include reserved _tool_receipt: host_seconds, harness_cache_hit,
sdk_rpc_seconds and (for jev/vision wrappers) sdk_total_seconds including image encoding.
context()["last_tool_receipt"] provides the latest receipt. Completed requests/results/timing/errors
are readable in evidence/visual_calls/. Host cache timing is included; measure your own OpenCV work.

Only submit runs games. Do not use Bash to launch a game, read private files, game source, DOM or device/CDP channels.
Generated policy runtime has no network or subprocess access; fixed vision RPC is the provided route.
Use assets/ for your own templates or tables derived from public observations.
Read actual frames/events/feedback after submit, then revise. At most eight submits, subject to shared output budget.
Every submit is a fresh game and a frozen policy/assets version. You may finish before eight submissions.
An external evaluator records scores separately; you do not receive those private values.

Trajectory files record selected frames, frames actually used by policy, submitted versus executed inputs,
missed windows, timing, resets and errors. Use these actual execution facts to diagnose changes.
