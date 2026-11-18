"""Claude Code test double for exercising the MCP and model transports."""

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request


def main():
    if os.environ.get("RGBGAME_TRUSTED_FAKE_FIXTURE") != "1":
        raise RuntimeError("fake CLI requires an explicit synthetic run")
    args = sys.argv[1:]
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    mcp = config["mcpServers"]["game"]
    review = "--review" in mcp["args"]
    session = args[args.index("--resume" if "--resume" in args else "--session-id") + 1]
    folder = Path(os.environ["CLAUDE_CONFIG_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    history = folder / (session + ".json")
    if "--resume" in args and not history.exists():
        raise RuntimeError("resume lost native fake session")
    messages = json.loads(history.read_text()) if history.exists() else []
    messages.append({"role": "user", "content": args[-1]})
    process = subprocess.Popen(
        [mcp["command"], *mcp["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    sequence = 0

    def rpc(method, params):
        nonlocal sequence
        sequence += 1
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": sequence, "method": method, "params": params})
            + "\n"
        )
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    try:
        rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fake", "version": "1"},
            },
        )
        tools = rpc("tools/list", {})["tools"]
        for _ in range(10):
            status = json.loads(
                rpc("tools/call", {"name": "status", "arguments": {}})["content"][0]["text"]
            )
            if status["output_budget"]["stop_reason"] or (
                not review and status["remaining_rounds"] == 0
            ):
                break
            body = {
                "model": "fake-developer",
                "system": "review" if review else "develop",
                "messages": messages,
                "max_tokens": 128,
                "stream": True,
                "tools": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["inputSchema"],
                    }
                    for t in tools
                ],
            }
            req = urllib.request.Request(
                os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
                data=json.dumps(body).encode(),
                headers={
                    "X-Api-Key": os.environ["ANTHROPIC_API_KEY"],
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                blocks, partial, complete = {}, {}, False
                for line in response:
                    if not line.startswith(b"data:"):
                        continue
                    event = json.loads(line[5:])
                    kind = event["type"]
                    if kind == "content_block_start":
                        blocks[event["index"]] = event["content_block"]
                    elif kind == "content_block_delta":
                        index, delta = event["index"], event["delta"]
                        if delta["type"] == "input_json_delta":
                            partial[index] = partial.get(index, "") + delta["partial_json"]
                        else:
                            key = "thinking" if delta["type"] == "thinking_delta" else "signature"
                            blocks[index][key] = blocks[index].get(key, "") + delta[key]
                    elif kind == "message_stop":
                        complete = True
                if not complete:
                    raise RuntimeError("incomplete fake Messages stream")
                for index, value in partial.items():
                    blocks[index]["input"] = json.loads(value)
                answer = {"content": [blocks[i] for i in sorted(blocks)]}
            messages.append({"role": "assistant", "content": answer["content"]})
            call = next(c for c in answer["content"] if c["type"] == "tool_use")
            if not review:
                key = "right" if status["submitted_rounds"] % 2 == 0 else "left"
                Path("policy/policy.py").write_text(
                    "def step(frame, state):\n    assert frame.dtype.name == 'uint8' and frame.shape[2] == 3\n"
                    f"    return '(press({key}))', (state or 0) + 1\n"
                )
            feedback = rpc("tools/call", {"name": call["name"], "arguments": call["input"]})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": json.dumps(feedback),
                        }
                    ],
                }
            )
            history.write_text(json.dumps(messages))
            print(
                json.dumps({"type": "fake_test_turn", "session": session, "tool": call["name"]}),
                flush=True,
            )
            if feedback.get("isError"):
                raise RuntimeError(feedback)
            if review:
                break
    finally:
        process.stdin.close()
        process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
