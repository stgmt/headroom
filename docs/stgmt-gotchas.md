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

## 2026-07-15: Container-only RTK does not optimize host Claude Code

Problem:
The Headroom image contained RTK and a controlled container probe showed 97.55%
output reduction, but normal Claude Code traffic still reported zero automatic
RTK commands/savings. Claude Code executes Bash on Windows before Headroom sees
the tool result, so the container binary was outside the execution path.

First failed fix:
A global Claude `PreToolUse(Bash)` hook called the WSL RTK binary and passed a
direct PowerShell-to-WSL synthetic probe. In a real Claude Code process the hook
still did nothing. Claude's debug log proved that Git Bash/MSYS changed
`/home/devcontainers/.local/bin/rtk` into
`C:/Program Files/Git/home/devcontainers/.local/bin/rtk`; the hook errored and
Claude continued with the original command.

Fix in the paired `stgmt/sub2api` profile:
- Install pinned RTK 0.42.4 on Windows and WSL.
- Prefix the WSL hook command with `MSYS2_ARG_CONV_EXCL='*'`.
- Probe the hook through Git Bash, not directly from PowerShell.
- Preserve `cat`, `git diff`, `git show`, and `curl` accuracy exclusions.
- Bind `%LOCALAPPDATA%\rtk` into Headroom at `/root/.local/share/rtk` so the
  dashboard reads the same persistent command history as the host.

Native Linux/Hyper-V variant:
- If Claude Code is installed on the Ubuntu VM host, install RTK in that same
  user account with `stgmt/sub2api` `scripts/install-claude-rtk.sh`. Installing
  it only in Headroom or a devcontainer repeats the original execution-path
  bug because those filesystems do not own the host Bash process.
- The installer preserves Claude settings and non-RTK hooks, leaves one
  absolute `PreToolUse(Bash)` hook, applies the same accuracy exclusions, and
  runs synthetic rewrite/exclusion probes before returning success.
- In a split-host VM -> remote Headroom topology, VM history is authoritative.
  Do not claim dashboard parity unless the VM RTK state is explicitly shared
  with the Headroom host.

Required proof:
- Claude debug contains `Hook PreToolUse:Bash (PreToolUse) success` and
  `modified tool input keys: [command, description]`.
- A real Claude Bash `git log -1 --oneline` creates a host history row with
  `rtk_cmd=rtk git log -1 --oneline`.
- An isolated fresh Claude `eslint tools/tui-test-runner/dispatch.ts` probe
  created history row 1082 with `rtk_cmd=rtk lint eslint ...`, reduced output
  from 61 to 6 tokens, and saved 90.16% without a manual `rtk` command.
- Host and container `rtk gain --format json` totals match.
- Live proof after wiring: 1,045 commands, 1,987,904 tokens saved, 72.8% average
  savings; `headroom perf --format json` reported the same values under
  `cli_filtering`. The earlier 97.55% remains a controlled capability probe,
  not the lifetime live savings rate.
- Native Ubuntu host proof on `devcontainer-ubuntu-2404`: a fresh Claude Code
  call rewrote `git log -100 --stat`; RTK accounted `51,221 -> 7,792`, saving
  84.8%, and a post-installer fresh Claude call incremented history `3 -> 4`.

Owned files:
- `stgmt/sub2api` `scripts/install-claude-rtk.ps1`
- `stgmt/sub2api` `scripts/install-claude-rtk.sh`
- `stgmt/sub2api` `scripts/verify-claude-code-sub2api.ps1`
- `stgmt/sub2api` `deploy/claude-code-codex-headroom/docker-compose.yml`
- `docs/rtk-architecture.md`
- `skills/headroom-sub2api-maintainer/SKILL.md`

## 2026-07-15: Mixed server-memory and client tool calls broke Claude Code

Problem:
Headroom can inject private `memory_*` tools into a Claude Code request. GPT-5.6
may call `memory_search` and a client-owned tool such as `Bash` in the same
assistant turn. The buffered Anthropic handler replayed every `tool_use` in its
internal continuation but generated a `tool_result` only for the memory call.
sub2api translated that incomplete turn to the OpenAI Responses protocol, which
rejected it with `400 No tool output found for function call call_*` before
Claude Code received its first real tool call.

The same prompt failed twice in the Hyper-V Ubuntu Claude host:
- session `aee9a005-acde-4a9e-a877-3152ea97623f` at `07:26:39Z`, missing call
  `call_WoOaTjIoJQ8N1Kier0lMB9Bl`;
- the retry at `07:47:42Z`, missing call
  `call_lzg6wbTB8J2TpkIHRi0VyPF4`.

Fix:
- Build the server-side continuation assistant turn from only those `tool_use`
  blocks whose IDs have matching memory `tool_result` blocks.
- Defer client-owned tool calls; because the buffered first response never
  reached Claude Code, the continuation can safely issue them again.
- Remove all private Headroom memory tool definitions from the continuation so
  the model cannot reissue a tool that Claude Code does not own.
- Refuse the internal continuation if a generated memory result has no matching
  assistant `tool_use`.

Files:
- `headroom/proxy/handlers/anthropic.py`
- `tests/test_anthropic_memory_mixed_tools.py`
- `tests/test_proxy/test_anthropic_buffered_timeout.py`

Required proof:
- Unit contract tests cover mixed `memory_search + Bash`, unmatched result IDs,
  and removal of private memory definitions.
- A live forced mixed call from the Ubuntu VM logs
  `Memory: Deferred 1 client-owned tool call(s) from continuation: ['Bash']`.
- The continuation and all following sub2api `/v1/messages` calls return HTTP
  200 with no `openai_messages.forward_failed`.
- Claude Code reports both the memory result and the Bash result instead of an
  API error or `memory_search` unavailable.

Live proof after the fix:
- Headroom request `hr_1784103012_000005` deferred Bash and completed its
  continuation successfully.
- The forced VM probe returned `memory_search: совпадений нет` and
  `pwd: /home/migration`.

## 2026-07-16: Default 60 RPM limiter broke local Claude fan-out

Problem:
Headroom enables a 60 requests/minute token bucket by default. The Anthropic
handler keys that bucket by the first 16 characters of the API key plus client
IP. Every local Claude Code window and subagent used the same placeholder key
and loopback address, so unrelated agents depleted one shared bucket. Headroom
returned `429 {"detail":"Rate limited. Retry after 0.3s"}` before sub2api saw
the request. Claude Code exposed the failure inside a WebSearch tool result and
did not replay the lost tool call.

Evidence:
- Headroom's lifetime `headroom_requests_rate_limited_total` was 1009.
- Completed traffic reached 68, 74, 63, and 57 requests in consecutive minutes;
  rejected requests were not present in the request JSONL.
- A controlled 96-way invalid-key burst against the old live profile returned
  exactly 60 HTTP 401 responses and 36 local HTTP 429 responses. The invalid
  key kept the probe out of model billing while exercising the real limiter.

Fix in the paired `stgmt/sub2api` loopback profile:
- Set `HEADROOM_RPM=6000` and `HEADROOM_TPM=100000000` in compose and generated
  `.env` files. Keep these values configurable for operators.
- Verify effective values through Headroom `/stats`; source or container-env
  inspection alone is insufficient.
- Run the repository-owned 96-way invalid-key burst probe after recreating the
  Headroom service. Any HTTP 429 is a regression.

Do not classify this exact FastAPI JSON body as an OpenAI subscription cooldown.
Real upstream quota errors are recorded by sub2api and have different account,
usage-log, and error-log evidence.

## 2026-07-18: Output shaper silently downgraded Claude max effort

Problem:
Claude Code correctly sent `output_config.effort=max` on every turn, and its
JSONL labeled every assistant message `effort=max`. The paired Headroom profile
enabled `HEADROOM_OUTPUT_SHAPER=1` but omitted `HEADROOM_EFFORT_ROUTER`.
Headroom therefore used the router's default-on behavior, classified clean
`tool_result` turns as mechanical continuations, and rewrote `max` to `low`
before forwarding them to sub2api.

Evidence before the fix:
- Exact timestamp plus token-usage matching found 181 JSONL responses labeled
  `max`; sub2api recorded only 61 as `max` and 120 as `low`.
- A four-request VM tool-loop wire tap saw `max` on all four requests before
  Headroom. sub2api recorded the first as `max` and the next three as `low`.
- The downgrade implementation is `route_effort()` in
  `headroom/proxy/output_shaper.py`; its default target is `low`.

Fix in the paired `stgmt/sub2api` profile:
- Keep `HEADROOM_OUTPUT_SHAPER=1` for verbosity steering.
- Set `HEADROOM_EFFORT_ROUTER=0` explicitly in compose, generated `.env`, the
  installer, and the live runtime.
- Make the verifier fail if the effective container environment does not
  contain both values.

Required live proof:
- Capture a real Claude Code `--effort max` tool-loop before Headroom.
- Match every request to sub2api `usage_logs` by completion time and token use.
- Both sides must report `max` for every initial and continuation request. The
  post-fix four-turn probe was `4/4 max` before and after Headroom.

## 2026-07-22: sub2api Bearer keys must route as Anthropic-compatible

Problem:
The paired Claude Code stack uses Headroom in front of sub2api and authenticates
Claude Code with a local placeholder key such as `Bearer sk-sub2api...`. That key
is not an OpenAI key. If Headroom classifies any `sk-*` Bearer token as OpenAI,
`/v1/models` and catch-all proxy calls are sent to the OpenAI-compatible route
and fail with an upstream-style `Incorrect API key provided: sk-sub2a***`
instead of reaching sub2api's Anthropic-compatible `/v1/messages` path.

Evidence:
- The live container originally had no `sk-sub2api` branch in
  `headroom.providers.registry._is_anthropic_auth` or
  `headroom.proxy.helpers.is_anthropic_auth`.
- After patching the live site-packages copy,
  `is_anthropic_auth({"authorization":"Bearer sk-sub2api-test"})` returned
  `True`.
- A Headroom `/v1/models` probe returned a mixed provider catalog that included
  `gpt-5.6-sol`, `qwen3.8-max-preview`, and `fable`.

Fix:
- Treat `Authorization: Bearer sk-sub2api...` as Anthropic-compatible in both
  the provider registry and proxy helper classifier.
- Cover both `/v1/models` and catch-all proxy routing with regression tests.
- Rebuild/recreate the Headroom image from the fork; copying patched files into
  a running container is only an emergency proof, not a durable install.

## 2026-07-22: disable CPU Kompress when CUDA proof is absent

Problem:
The sub2api Docker profile can auto-detect no usable Docker GPU while leaving
`HEADROOM_FORCE_KOMPRESS=1`. In that state Headroom may use CPU ONNX Kompress on
Claude Code's hot path, which previously caused multi-minute compression stalls
and request watchdog failures. A Docker container being healthy is not enough;
the important proof is the effective compression backend.

Fix in the paired `stgmt/sub2api` profile:
- For CUDA profile: `HEADROOM_FORCE_KOMPRESS=1`,
  `HEADROOM_DISABLE_KOMPRESS=0`, `HEADROOM_KOMPRESS_BACKEND=pytorch`.
- For CPU/no-GPU profile: `HEADROOM_FORCE_KOMPRESS=0`,
  `HEADROOM_DISABLE_KOMPRESS=1`. Re-enable Kompress only after a live CUDA
  preload proof succeeds.

Required live proof:
- `docker inspect headroom-sub2api` shows the intended environment.
- `GET /health` reports `kompress.enabled=false`, `status=disabled`, and
  no growing compression queue for CPU/no-GPU installs; for CUDA installs,
  benchmark and preload must prove `pytorch` on `cuda`.

## 2026-07-23: `/stats` runtime counters hid persisted request history

Problem:
The request JSONL survived container recreation on the `/root/.headroom` bind
mount, but `RequestLogger` started with an empty deque and `/stats.requests`,
`/stats.tokens`, and `/stats.latency` read process-local metrics. After a
restart the dashboard/API therefore looked almost empty even though tens of
thousands of completed requests remained in `proxy-requests.jsonl`.

Evidence before the fix:
- Live `/stats.requests.total` showed 341 requests for the current process.
- `persistent_savings.lifetime.requests` showed 63,193 requests.
- The bind-mounted request log contained 63,621 valid, unique request IDs from
  2026-07-10 through 2026-07-23 plus one malformed line.

Fix:
- `RequestLogger` streams the JSONL once at startup, skips malformed rows,
  deduplicates request IDs, restores the bounded recent-request feed, and
  maintains a JSON-safe lifetime aggregate as new rows are appended.
- `/stats.request_history` exposes completed-request totals, provider/model
  breakdowns, tokens, latency, transforms, coverage, and source time range.
- Existing top-level request/token/latency fields remain runtime-scoped for
  compatibility and expose their durable blocks under `lifetime`.
- `/stats/reset` remains runtime-only and never truncates the request log.

Required live proof:
- Record request-history totals, restart/recreate Headroom, and verify totals
  and range are unchanged before new traffic.
- Send one fresh request and verify exactly one new unique request and no
  double counting after a second restart.
- Keep the request-log path on the `/root/.headroom` host bind mount.

## Sync Rule

When this fork changes a behavior used by the sub2api Docker profile, update the sub2api stack too:
- `deploy/claude-code-codex-headroom/*`
- `backend/docs/skills/sub2api-claude-code-codex/*`
- local installed skill at `%USERPROFILE%\.codex\skills\sub2api-claude-code-codex`

Do not claim a Headroom fix is active until the running `headroom-sub2api` container proves it.
