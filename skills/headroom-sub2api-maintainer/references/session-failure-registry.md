# Headroom Session Failure Registry

This is the Headroom-owned subset of the cross-stack incident registry. The
canonical full registry lives in `stgmt/sub2api` at
`backend/docs/skills/sub2api-claude-code-codex/references/session-failure-registry.md`.

Use the same IDs in issues, tests, commit messages, and reports.

| ID | Headroom failure class | Required behavior and proof |
|---|---|---|
| `F02` | Source fix was not active in the running image. | Rebuild/recreate, inspect image source/revision and creation time, then reproduce through Claude Code. |
| `F05` | Native `/compact` marker was transformed away. | Preserve exact compact message, add `x-sub2api-claude-compact: 1`, skip shaping; prove marker plus downstream compact route. |
| `F06` | Accepted compact immediately refilled or thrashed. | Compare pre/post context and first post-compact payload; attribute rehydrated/tool content before changing compact routing. |
| `F07` | Output shaper silently changed explicit effort. | Keep `HEADROOM_EFFORT_ROUTER=0`; prove the wire body and downstream usage row, not the Claude label. |
| `F12` | Default local 60 RPM bucket interrupted healthy fan-out. | Use the loopback profile's high RPM/TPM and prove effective `/stats` plus a burst probe. |
| `F13` | Empty/malformed/incomplete SSE stopped an agent. | Validate Anthropic event order; distinguish pre-event from mid-stream failure; replay only safe pre-event requests and prove one complete continuation. |
| `F14` | Handler hung or leaked a 504 to Claude Code. | Whole-handler watchdog cancels primary work and retries once through safe bypass; never fail open with an oversized prompt. Prove bounded wall time and no leaked task. |
| `F15` | context-mode MCP hung and was mistaken for Headroom. | Correlate MCP and proxy timestamps first. Absence of a Headroom request is evidence to debug the plugin, not mutate proxy routing. |
| `F16` | Mixed server memory and client tools caused `No tool output found`. | Defer client-owned calls and replay only result-backed memory calls; prove deferred marker and successful continuation. |
| `F18` | Container RTK existed but live host savings stayed zero. | Install/hook RTK in the Claude Bash host OS/user; require a fresh host history increment. |
| `F20` | GPU was claimed from host/device config while Kompress used CPU or was disabled. | Require Docker `DeviceRequests`, Torch CUDA/device, `preload=pytorch`, benchmark sentinel retention, and live compression metrics. |
| `F21` | Reboot or bare compose silently downgraded a proven CUDA profile. | Sticky CUDA plus GPU overlay in setup/start/autostart; failed inspect fails closed. Re-run CUDA proof after the actual scheduled task. |
| `F22` | Multiple launchers or named volumes split ownership and state. | One canonical stack owner and host bind mounts; prove task state, mounts, and persisted files after restart. |
| `F23` | Container health was green while the Claude host route refused connections. | Probe every network hop used by the actual host before touching Headroom internals. |
| `F24` | Focused/source tests were called end-to-end proof. | Pair focused tests with a rebuilt running revision, negative control, and original-client reproduction; respect analysis-only requests. |
| `F25` | `/stats` looked empty after restart although request logs survived. | Hydrate `request_history` from the persisted JSONL, keep runtime scope explicit, and prove stable totals across two restarts plus an exact +1 after fresh traffic. |
| `F37` | A real native Anthropic subscription cooldown or a temporary sub2api/network outage terminated main, compact, or subagent turns although the containers stayed healthy. | Preserve `stream:true` with an HTTP 200 Anthropic SSE hold and preserve buffered `stream:false` with an HTTP 200 chunked JSON hold whose whitespace keepalives precede the recovered JSON. Use full `Retry-After`, route-scoped circuit/singleflight, and bounded retries for configured 429/502/503/504/529 plus transport failures. Keep an initially non-retryable 400/401 visible and never cross providers in `anthropic-only`. Prove both wire formats, health recovery counters, and live recovery. |
| `F38` | One Sonnet/Opus 429 made later GPT/Qwen turns wait in five-minute cycles although sub2api served the requested model normally on retry. | Key Headroom cooldown and probe state by upstream URL, credential, and requested model. Clear the matching model circuit on every successful streaming or buffered response. Prove a deferred Claude model leaves GPT/Qwen at zero cooldown, preserve same-model singleflight, and verify alternating streaming/buffered paths cannot retain stale state. |
| `F40` | A normal turn switched to the compact model after a skill/tool result mentioned `Context compaction`. | Headroom must be the only compact classifier on this path: preserve the exact native compact prompt and send `x-sub2api-claude-compact: 1`. sub2api must not infer compact from serialized request text. Prove marker text in every message role stays on main and a real native compact carries the header. |
| `F41` | Structured fields survived but the user's explicit task disappeared; a screenshot of the same task worked. | The `agent-90` profile must keep user/system/developer instructions byte-exact and reserve lossy compression for tool/output material. Prove the incident-shaped task suffix survives while a large tool result still compresses, then verify effective runtime compression env is zero for instruction roles. |

When a new Headroom incident does not fit an existing ID, add it first to the
canonical sub2api registry, mirror the owned subset here, update
`docs/stgmt-gotchas.md`, and add a focused regression test.
