"""Public RGB-only SDK and command line tools copied into the developer workspace."""

import argparse
import base64
import io
import json
import os
import socket
import time
import uuid
from pathlib import Path


_context = {}


def context():
    """Own timing, queue and actual input receipts for this RGB observation."""
    return dict(_context)


def rpc(operation, **arguments):
    started = time.monotonic()
    request = {
        "operation": operation,
        "call_id": uuid.uuid4().hex,
        "context": {k: _context.get(k) for k in ("frame_id", "epoch", "tick", "received_ns")},
        "arguments": arguments,
        "token": os.environ["GAME_TOOL_TOKEN"],
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1800)
        sock.connect(os.environ["GAME_TOOL_SOCKET"])
        stream = sock.makefile("rwb")
        stream.write((json.dumps(request, allow_nan=False) + "\n").encode())
        stream.flush()
        result = json.loads(stream.readline(32 * 1024 * 1024))
    if operation in {"jev", "vision"}:
        value = result.get("result")
        receipt = dict(value.get("_tool_receipt", {})) if isinstance(value, dict) else {}
        receipt.update(
            call_id=request["call_id"],
            sdk_rpc_seconds=time.monotonic() - started,
            operation=operation,
            error=result.get("error"),
        )
        _context["last_tool_receipt"] = receipt
        with (Path(os.environ["HOME"]) / ".game_tool_receipts.jsonl").open("a") as out:
            out.write(json.dumps(receipt, allow_nan=False) + "\n")
        if isinstance(value, dict):
            result["result"] = {**value, "_tool_receipt": receipt}
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "tool_failed"))
    return result["result"]


def image_data(frame):
    from PIL import Image

    if isinstance(frame, str | Path):
        with Image.open(frame) as im:
            image = im.convert("RGB")
    else:
        image = Image.fromarray(frame, mode="RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def jev(frame, questions, instructions="Answer from the supplied RGB image only."):
    """questions: {id: {type:'choice', instructions:str, criteria:{label:description}}}."""
    started = time.monotonic()
    value = rpc("jev", image=image_data(frame), questions=questions, instructions=instructions)
    if isinstance(value, dict) and "_tool_receipt" in value:
        value["_tool_receipt"]["sdk_total_seconds"] = time.monotonic() - started
    return value


def vision(frame, kind, **parameters):
    """kind is ocr, sam, or depth; returns JSON, depth returns base64 NPZ."""
    started = time.monotonic()
    value = rpc("vision", image=image_data(frame), kind=kind, parameters=parameters)
    if isinstance(value, dict) and "_tool_receipt" in value:
        value["_tool_receipt"]["sdk_total_seconds"] = time.monotonic() - started
    return value


def crop(path, bbox, output=None):
    from PIL import Image

    box = [int(v) for v in bbox]
    with Image.open(path) as im:
        if not (
            len(box) == 4 and 0 <= box[0] < box[2] <= im.width and 0 <= box[1] < box[3] <= im.height
        ):
            raise ValueError("bbox must be pixel [left,top,right,bottom] inside the image")
        dest = Path(output or ("assets/crop-" + uuid.uuid4().hex[:10] + ".png"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.crop(box).save(dest)
    return {"path": str(dest.resolve()), "bbox": box}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["status", "submit", "crop", "jev", "vision"])
    parser.add_argument("--path")
    parser.add_argument("--bbox", nargs=4, type=int)
    parser.add_argument("--output")
    parser.add_argument("--json", default="{}", help="JSON arguments or @file.json")
    args = parser.parse_args()
    if args.operation == "crop":
        result = crop(args.path, args.bbox, args.output)
    else:
        body = json.loads(
            Path(args.json[1:]).read_text() if args.json.startswith("@") else args.json
        )
        if args.path:
            body["image"] = image_data(args.path)
        result = rpc(args.operation, **body)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
