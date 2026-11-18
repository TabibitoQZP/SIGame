"""Clients for the CV and Jev visual services."""

import base64
import requests
from gpa.config import endpoint
from .vision_frame_client import FrameClient


def perceive(config, op, args):
    raw = base64.b64decode(args["image"], validate=True)
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise ValueError("image must be at most 16 MiB")
    if op == "jev":
        url, key = endpoint(config, "jev")
        body = {
            "model": config.get("jev_model", "vev-4b"),
            "state": {
                "context": args.get("instructions", "Answer from RGB only."),
                "screen": {"image": {"url": "data:image/png;base64," + args["image"]}},
            },
            "questions": args["questions"],
        }
        response = requests.post(
            url.rstrip("/").removesuffix("/v1/systemone") + "/v1/systemone",
            headers={"Authorization": "Bearer " + key},
            json=body,
            timeout=180,
        )
        response.raise_for_status()
        return response.json()
    kind = args["kind"]
    if kind not in {"sam", "depth", "ocr"}:
        raise ValueError("unknown vision operation")
    url, key = endpoint(config, "cv")
    with FrameClient(url, api_key=key, timeout=180) as client, client.upload_image(raw) as frame:
        if kind == "depth":
            meta, payload = frame.depth_npz_with_metadata(**args.get("parameters", {}))
            return {
                "npz_base64": base64.b64encode(payload).decode(),
                **{
                    k: meta[k]
                    for k in ("model", "width", "height", "depth_units", "shape", "timings")
                    if k in meta
                },
            }
        return getattr(frame, kind)(**args.get("parameters", {}))
