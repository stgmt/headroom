# stgmt Headroom Maintenance

Fork:
`https://github.com/stgmt/headroom`

Upstream:
`https://github.com/headroomlabs-ai/headroom`

Purpose:
Keep the Claude Code + Headroom + sub2api/Codex subscription path reliable while upstream catches up. This fork tracks local production fixes, regression tests, gotchas, and the maintainer skill used to avoid rediscovering the same failures.

## Current Downstream Patches

- Claude Code stream identity: `x-claude-code-session-id` + `x-claude-code-agent-id`.
- Claude Code mid-turn overlap wait instead of private 202 queue response.
- Active-stream refcounts.
- Claude Code handler watchdog with one bypass/passthrough retry before 504.
- Embedding server sidecar module and socket embedder factory path.
- Optional CUDA PyTorch Kompress runtime through the sub2api GPU image stage and compose overlay.

## Verification Commands

Run focused tests:

```powershell
python -m pytest tests/test_stgmt_claude_code_recovery.py tests/test_cli_proxy_embedding_server.py tests/test_mid_turn_steering.py
```

Static checks:

```powershell
python -m py_compile headroom/proxy/handlers/anthropic.py headroom/proxy/handlers/streaming.py headroom/memory/adapters/watchdog.py headroom/memory/factory.py
```

Runtime proof lives in the sub2api stack verifier:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/docs/skills/sub2api-claude-code-codex/scripts/verify-claude-code-sub2api.ps1 -BaseUrl http://<wsl-ip>:8787 -SkipApiProbe -SkipClaudeProbe
```

Expected runtime marker:
`WATCHDOG_RETRY_OK attempts=2 response=WATCHDOG_RETRY_RESPONSE`

CUDA profile proof:

The source-level rationale, benchmark method, autostart failure, and ownership
boundary are recorded in `docs/stgmt-gpu-kompress.md`.

```powershell
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api python -c "import torch; from headroom.transforms.kompress_compressor import KompressCompressor; print(torch.cuda.is_available(), torch.cuda.get_device_name(0), KompressCompressor().preload(allow_download=False))"
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api benchmark-headroom-kompress --require-cuda
```

The 2026-07-14 RTX 4070 SUPER A/B used the same 8 x 1400-word payload and
retained all 664 sentinels. CPU ONNX median was 24.1358 seconds; CUDA PyTorch
median was 0.5202 seconds, a 46.4x speedup for that batched fixture. Do not
generalize that multiplier without rerunning the fixture on the target GPU.

## Upstream Sync Checklist

1. Fetch upstream `headroomlabs-ai/headroom`.
2. Rebase `stgmt/main` on upstream `main`.
3. Re-run focused tests.
4. Rebuild the downstream Docker image in `sub2api`.
5. Re-run the sub2api verifier against the actual Claude Code `ANTHROPIC_BASE_URL`.
6. Update `docs/stgmt-gotchas.md` with any new behavior, date, and proof.

## Do Not Lose These Decisions

- A clean 504 is not enough for Claude Code agent survival; retry first.
- A TCP-open localhost port is not proof that Windows can reach the proxy HTTP app.
- The Docker runtime is the source of truth for whether a patch is active.
- Keep gotchas in this repo and mirror operational instructions into the sub2api skill when they affect installation or verification.
- GPU enablement belongs to the sub2api Docker profile: Headroom already implements the PyTorch CUDA batch path, while the image/compose must provide CUDA Torch and GPU devices.
