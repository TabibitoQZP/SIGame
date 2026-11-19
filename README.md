# SIGame

*Self-Improvement in Real-Time Visual Games*

SIGame explores how agents can improve their gameplay policies through interaction with real-time visual games.

The goal is to turn experience from a limited number of attempts into more effective policies. Agents observe the game through screenshots and act through keyboard and mouse inputs, while the game continues to run during decision-making.

**Paper**: [Think Offline, Act in Real Time: Self-Improving Visual Game Agents through Code](./SIGame.pdf)

## Setup

Use Python 3.12 and uv on a Linux host with a systemd user session, cgroup v2, Docker Engine and Buildx. The local RTC model also requires an NVIDIA GPU and the NVIDIA Container Toolkit.

```bash
uv sync --frozen
sudo install -m 644 docker/rgbgame-eval.slice /etc/systemd/system/rgbgame-eval.slice
sudo systemctl daemon-reload
sudo systemctl start rgbgame-eval.slice
uv run rgbgame gameworld prepare
systemd-run --user --scope --quiet -p MemoryMax=1920M -p MemorySwapMax=0 \
  uv run python tools/build_runtime.py --output outputs/build
```

Both examples below use the same 2048 task, `01_05`. Choose a fresh output directory for each run.

## Model as policy

The RTC controller uses screenshots and the pending action queue to plan its next actions. Download the [RTC-2B checkpoint](https://modelscope.cn/models/TabibitoQZP/Qwen3.5-2B-RTC32-RL-STEP229) to a local directory.

```bash
bash tools/serve_rtc.sh /path/to/rtc-step229
uv run rgbgame services up --output-token-limit 524288
uv run rgbgame run --config configs/rtc.json --task 01_05 --output outputs/rtc-2048
uv run rgbgame services down
docker stop rgbgame-rtc-sglang && docker rm rgbgame-rtc-sglang
```

## Code as policy

A coding agent writes a Python policy, plays a one-minute game with it, and uses the screenshots and execution record to revise the next version. Set `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` for a Claude Code compatible Messages endpoint with image input, streaming, tool use and token counting. Set `developer_model` in the config to the model ID served by the endpoint.

```bash
uv run rgbgame services up --output-token-limit 262144
uv run rgbgame run --config configs/single_agent.json --task 01_05 --output outputs/code-2048
uv run rgbgame services down
```

For four developers with a midpoint peer review, use `configs/multi_agent.json` and start the services with `--output-token-limit 1048576`.

Games and scoring are provided by [GameWorld](https://github.com/gameworld-project/GameWorld). The game assets are fetched from [GameWorld-Games](https://github.com/gameworld-project/GameWorld-Games). The optional visual client comes from CVAgent.

## Citation

```bibtex
@misc{qiu2026thinkofflineactreal,
  title         = {Think Offline, Act in Real Time: Self-Improving Visual Game Agents through Code},
  author        = {Zipeng Qiu and Shusheng Xu and Jiale Li and Yukun Zhou and Chenyue Li and Wenshuang Li and Tongkai Yang and Binhang Yuan},
  year          = {2026},
  eprint        = {2611.24590},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2611.24590},
}
```
