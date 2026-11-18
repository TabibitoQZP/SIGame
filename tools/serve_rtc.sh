#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s /path/to/rtc-step229\n' "$0" >&2
  exit 2
fi
checkpoint=$(realpath -e -- "$1")
if [[ ! -r "$checkpoint/config.json" ]]; then
  printf 'Expected an unpacked checkpoint directory containing config.json.\n' >&2
  exit 2
fi

image=lmsysorg/sglang:v0.5.18@sha256:9e148f5ac788e856a06166bd6347a831831eb9fcfab4d1770874823a7c29a1a1
model=Qwen3.5-2B-RTC32-RL-STEP229
cache=${XDG_CACHE_HOME:-"$HOME/.cache"}/rgbgame/sglang-0.5.18
mkdir -p -- "$cache"

docker run --detach --pull missing --name rgbgame-rtc-sglang \
  --gpus device=0 --user "$(id -u):$(id -g)" --cap-drop ALL \
  --security-opt=no-new-privileges --cgroup-parent=rgbgame-eval.slice \
  --memory=9g --memory-swap=9g --cpus=6 --pids-limit=512 --shm-size=2g \
  --publish 127.0.0.1:18001:8000 \
  --mount "type=bind,source=$checkpoint,destination=/model,readonly" \
  --mount "type=bind,source=$cache,destination=/runtime-cache" \
  -e HOME=/tmp -e XDG_CACHE_HOME=/runtime-cache \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HF_HUB_DISABLE_TELEMETRY=1 \
  -e OMP_NUM_THREADS=2 -e MAX_JOBS=2 \
  --entrypoint python3 "$image" -m sglang.launch_server \
  --model-path /model --served-model-name "$model" \
  --host 0.0.0.0 --port 8000 --dtype bfloat16 --enable-multimodal \
  --reasoning-parser qwen3 --context-length 32768 --mem-fraction-static 0.68 \
  --max-running-requests 1 --chunked-prefill-size 4096 --cuda-graph-max-bs-decode 1

for ((attempt = 0; attempt < 150; attempt++)); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:18001/health >/dev/null; then
    printf '%s\n' 'STEP229 is ready at http://127.0.0.1:18001/v1'
    exit 0
  fi
  if [[ $(docker inspect --format '{{.State.Running}}' rgbgame-rtc-sglang) != true ]]; then
    docker logs --tail 50 rgbgame-rtc-sglang >&2
    exit 1
  fi
  sleep 2
done
printf '%s\n' 'SGLang startup timed out. Inspect docker logs rgbgame-rtc-sglang.' >&2
exit 1
