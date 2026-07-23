# GPU Kompress Deployment Record

## Repository Boundary

Headroom owns the Kompress implementation and backend selection. The
[`stgmt/sub2api`](https://github.com/stgmt/sub2api) fork owns the Claude Code
Docker image, Compose overlay, persistent accelerator selection, autostart, and
deployment verification. Do not duplicate the compression algorithm in the
deployment repo.

Public implementation links:

- [`stgmt/sub2api@3262db50`](https://github.com/stgmt/sub2api/commit/3262db50)
- [`stgmt/headroom@6112ecf6`](https://github.com/stgmt/headroom/commit/6112ecf6)
- [`stgmt/dev-pomogator#111`](https://github.com/stgmt/dev-pomogator/issues/111)

## Source-Level Findings

- `headroom/transforms/kompress_compressor.py` accepts
  `HEADROOM_KOMPRESS_BACKEND`.
- The ONNX session config selects `CPUExecutionProvider`.
- The PyTorch loader selects CUDA when `torch.cuda.is_available()`.
- `KompressCompressor.compress_batch` is the existing batched execution path.

Therefore Docker GPU exposure alone is insufficient. The container also needs
a CUDA-enabled PyTorch wheel and `HEADROOM_KOMPRESS_BACKEND=pytorch`.

Primary external references:

- [Docker Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/)
- [PyTorch 2.11 CUDA 12.8 installation](https://pytorch.org/get-started/previous-versions/)

## Live-Proven Profile

The sub2api profile keeps a portable `cpu` image target and adds an opt-in
`gpu` target with `torch==2.11.0+cu128`. Its GPU Compose overlay requests all
GPUs, selects the PyTorch backend, and overrides stale portable
`HEADROOM_FORCE_KOMPRESS=0` / `HEADROOM_DISABLE_KOMPRESS=1` values. A persisted
`HEADROOM_ACCELERATOR=cuda` selection is sticky: `auto` may detect and upgrade
to CUDA, but only an explicit `cpu` choice may downgrade it. Runtime proof
requires all of:

1. non-empty Docker `DeviceRequests`
2. `torch.cuda.is_available() == true`
3. expected device name
4. Kompress preload returns `pytorch`
5. deterministic benchmark completes with full sentinel retention

On an NVIDIA GeForce RTX 4070 SUPER, the identical 8 x 1,400-word fixture
measured 24.1358 seconds on CPU ONNX and 0.5202 seconds on CUDA PyTorch. Both
produced 2,408 output tokens from 11,200 input tokens and retained 664/664
sentinels. The 46.4x fixture result is not a universal performance guarantee.

## Operational Proof

```powershell
wsl.exe -d Ubuntu-24.04 -- docker inspect headroom-sub2api --format "{{json .HostConfig.DeviceRequests}}"
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api python -c "import torch; from headroom.transforms.kompress_compressor import KompressCompressor; print(torch.cuda.is_available(), torch.cuda.get_device_name(0), KompressCompressor().preload(allow_download=False))"
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api benchmark-headroom-kompress --require-cuda
```

The Windows autostart path must also be tested. A 2026-07-14 failure showed
that WSL could return UTF-16/NUL `Wsl/Service/0x8007274c` after an earlier WSL
command succeeded. The sub2api start script now normalizes the text and retries
that transient class on every WSL command. The real post-fix Scheduled Task
ended with `LastTaskResult=0` and retained CUDA.

Do not use a bare `docker compose up` launcher for a CUDA installation. The
canonical setup/recovery scripts select `docker-compose.gpu.yml`; the overlay
is the runtime ownership boundary for GPU DeviceRequests and Kompress enable
flags.

## Limits

GPU acceleration does not close the Headroom work for partial-result
compression, per-session singleflight, or a strict oversized unsafe-fail-open
policy. Keep those items open in `stgmt/dev-pomogator#111` until each has code
and behavioral tests.
