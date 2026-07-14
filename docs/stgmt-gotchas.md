# stgmt Headroom Gotchas

This file tracks downstream Headroom behavior that was proven against Claude Code + sub2api + Codex/OpenAI subscription routing.

## 2026-07-13: Claude Code must never receive Headroom private 202 queue responses

Problem:
Claude Code sends Anthropic `/v1/messages` streaming requests and expects Anthropic SSE events. Headroom's private mid-turn path returned HTTP 202 with `{"event":"headroom_queued"}` when another stream had the same internal session key. Claude Code does not implement that private queue protocol and surfaces this as stream/API failures.

Fix:
- Derive the stream key from `x-claude-code-session-id` plus `x-claude-code-agent-id` when `x-headroom-session-id` is absent.
- Replace the unsafe 202 branch with a bounded wait for the active stream to close.
- Keep active-stream refcounts so an older stream cleanup cannot clear a newer active marker.

Source:
- `headroom/proxy/handlers/anthropic.py`
- `headroom/proxy/handlers/streaming.py`
- `tests/test_stgmt_claude_code_recovery.py`

Required proof:
- `inspect.getsource(anthropic._sub2api_original_handle_anthropic_messages)` must not contain `return JSONResponse(content=queued, status_code=202)`.
- The source must contain `mid_turn_overlap_wait`.

## 2026-07-13: Handler watchdog must retry through bypass before returning 504

Problem:
When Headroom stalls inside `handle_anthropic_messages` before reaching upstream, Claude Code can wait for tens of minutes and eventually kills the agent with a synthetic timeout. Returning a clean `504 event:error` is not enough because it still kills the agent.

Fix:
- For Claude Code requests, run the Anthropic handler under `HEADROOM_CLAUDE_CODE_HANDLER_WATCHDOG_MS` (default `540000`).
- On primary timeout, cancel the primary task and retry the same request once with:
  - `x-headroom-bypass: true`
  - `x-headroom-mode: passthrough`
  - `x-sub2api-headroom-watchdog-retry: 1`
- Return 504 Anthropic SSE only if the bypass retry also times out.

Required proof:
- Tests should print or assert `WATCHDOG_RETRY_OK` semantics: attempt 1 times out, attempt 2 sees bypass headers and returns.
- Logs should show `event=claude_code_handler_watchdog_timeout`, then `event=claude_code_handler_watchdog_retry`, ideally `event=claude_code_handler_watchdog_retry_ok`.

## 2026-07-13: Embedding server flag must not silently fall back forever

Problem:
The CLI exposes `headroom proxy --embedding-server`, but the packaged 0.31.0 wheel used in the sub2api Docker stack imported `headroom.memory.adapters.watchdog`, which was absent. Startup fell back to per-worker embedders and lost the intended memory/RSS benefit.

Fix:
- Add `headroom/memory/adapters/watchdog.py`.
- Add `SocketEmbedderClient` and `EmbeddingServerWatchdog`.
- Teach `headroom.memory.factory._create_embedder` to return `SocketEmbedderClient` when `HEADROOM_EMBEDDING_SERVER_SOCKET` is set and the process is not the sidecar child.

Required proof:
- `from headroom.memory.adapters.watchdog import EmbeddingServerWatchdog, SocketEmbedderClient` succeeds.
- `HEADROOM_EMBEDDING_SERVER_SOCKET=/tmp/headroom-test.sock` makes `_create_embedder(MemoryConfig(embedder_backend=EmbedderBackend.ONNX))` return `SocketEmbedderClient`.

## 2026-07-13: Windows localhost can lie after WSL/Docker recreate

Problem:
After recreating the Docker service, Windows `127.0.0.1:8787` can connect at TCP level but hang on HTTP because an old `wslrelay.exe` owns the port. The container and WSL IP route can be healthy at the same time.

Operational rule:
- Verify both `http://127.0.0.1:8787/livez` and `http://<wsl-eth0-ip>:8787/livez`.
- If localhost hangs but WSL IP works, point Claude Code `ANTHROPIC_BASE_URL` at the WSL IP route until the Windows relay is healed.

Observed working route:
- `http://172.30.206.176:8787` on 2026-07-13.

## 2026-07-14: Docker GPU passthrough does not switch Kompress off CPU ONNX

Problem:
The host and WSL can see an NVIDIA GPU, and a container can have `gpus: all`,
while Headroom still runs Kompress on CPU. The standard image installs
`onnxruntime` only, and the ONNX backend explicitly selects
`CPUExecutionProvider`. It does not contain CUDA PyTorch.

Fix in the `stgmt/sub2api` profile:
- Keep the default CPU image stage for portable installs.
- Add a GPU image stage with pinned CUDA PyTorch.
- Apply `docker-compose.gpu.yml`, which requests all GPUs and sets
  `HEADROOM_KOMPRESS_BACKEND=pytorch`.
- Persist `HEADROOM_ACCELERATOR=cuda` so the single autostart owner reapplies
  the GPU overlay after reboot.

Required proof:
- Docker inspect shows non-empty GPU `DeviceRequests`.
- `torch.cuda.is_available()` is true and reports the expected GPU.
- `KompressCompressor().preload(allow_download=False)` returns `pytorch` and
  the loaded model device is `cuda`.
- `benchmark-headroom-kompress --require-cuda` succeeds with full sentinel
  retention.

Measured on RTX 4070 SUPER, identical 8 x 1400-word fixture:
- CPU ONNX: 24.1358 seconds median, 464.04 input tokens/s.
- CUDA PyTorch: 0.5202 seconds median, 21,530.32 input tokens/s.
- Speedup: 46.4x; both retained 664/664 sentinels and produced the same 0.215
  compression ratio.

## 2026-07-14: Headroom transforms can hide native Claude Code compact requests

Problem:
A native Claude Code `/compact` uses the current session model in the UI and
sends its compact instruction as the final user message. Headroom detected that
request as an ordinary `new_user_ask`, compressed the final instruction, and
applied output shaping. sub2api therefore could not recognize the compact and
routed the request to Sol instead of the configured Spark compact model.

Fix:
- Detect the native compact anchors before any Headroom transform.
- Preserve and restore the exact final compact message after compression hooks.
- Add `x-sub2api-claude-compact: 1` to the downstream request.
- Skip Headroom output shaping for compact requests.
- Keep sub2api responsible for `Spark -> Luna` fallback. A transcript that
  contains image blocks makes Spark return HTTP 400 because it has no image
  input support; that exact compact failure must switch to Luna.

Files:
- `headroom/proxy/handlers/anthropic.py`
- `tests/test_stgmt_claude_code_recovery.py`
- paired sub2api handler: `backend/internal/service/openai_gateway_messages.go`

Required proof:
- Installed Headroom source contains `_is_claude_code_compact_request`,
  `x-sub2api-claude-compact`, and
  `headroom:claude_code_compact_prompt_preserved`.
- `proxy-requests.jsonl` records the preserved-prompt transform for a real
  forked Claude Code `/compact`.
- sub2api logs show `compact_model_unavailable_fallback` when Spark rejects
  image input, and the final successful `usage_logs` row uses
  `upstream_model=gpt-5.6-luna`.
- Live proof on 2026-07-14: a 222.5k-context fork compacted successfully in
  129.1 seconds; final Luna row used 142,766 input and 4,258 output tokens.

## Sync Rule

When this fork changes a behavior used by the sub2api Docker profile, update the sub2api stack too:
- `deploy/claude-code-codex-headroom/*`
- `backend/docs/skills/sub2api-claude-code-codex/*`
- local installed skill at `%USERPROFILE%\.codex\skills\sub2api-claude-code-codex`

Do not claim a Headroom fix is active until the running `headroom-sub2api` container proves it.
