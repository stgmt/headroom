---
name: headroom-sub2api-maintainer
description: Maintain the stgmt/headroom fork for Claude Code through Headroom + sub2api + Codex/OpenAI subscription routing. Use for Headroom fork syncs, native compact routing, Claude Code stream hangs, handler watchdog retry, embedding-server, CUDA/PyTorch Kompress, Windows/WSL or native Ubuntu/Hyper-V RTK wiring, split-host metrics, WSL/Docker routing, and keeping gotchas/tests/docs aligned with the sub2api Docker profile.
---

# Headroom sub2api Maintainer

Use this skill when work touches `stgmt/headroom`, the `headroom-sub2api` Docker image, or Claude Code routed through Headroom.

## Sources Of Truth

- Fork: `https://github.com/stgmt/headroom`
- Upstream: `https://github.com/headroomlabs-ai/headroom`
- Gotchas ledger: `docs/stgmt-gotchas.md`
- Maintenance guide: `docs/stgmt-maintenance.md`
- GPU research and deployment record: `docs/stgmt-gpu-kompress.md`; portable skill copy: `references/gpu-kompress.md`
- sub2api profile mirror: `stgmt/sub2api`, `deploy/claude-code-codex-headroom`, and `backend/docs/skills/sub2api-claude-code-codex`
- RTK installers owned by `stgmt/sub2api`: `scripts/install-claude-rtk.ps1` for Windows plus WSL and `scripts/install-claude-rtk.sh` for native Linux Claude hosts
- Local runtime profile on this machine: `C:\Users\stigm\Documents\Codex\2026-07-07\new-chat\work\sub2api-runtime`

## Critical Invariants

- Claude Code must not receive Headroom's private HTTP 202 `headroom_queued` response for streaming `/v1/messages`.
- Native Claude Code compact requests must retain their exact final compact message after Headroom transforms, must carry `x-sub2api-claude-compact: 1` downstream, and must skip output shaping. Otherwise sub2api sees an ordinary Sol request and compact routing silently stays on Sol.
- Claude Code stream keys must include both `x-claude-code-session-id` and `x-claude-code-agent-id` unless an explicit `x-headroom-session-id` is provided.
- A server-side memory continuation must include only `tool_use` blocks with matching Headroom-produced `tool_result` IDs. Defer client-owned calls and remove private `memory_*` definitions from the continuation; replaying a mixed turn creates OpenAI Responses `400 No tool output found for function call call_*` errors.
- Handler watchdog timeout must cancel the primary handler and retry once through Headroom bypass/passthrough before any 504 leaves the proxy.
- `--embedding-server` must use `headroom.memory.adapters.watchdog.SocketEmbedderClient` when `HEADROOM_EMBEDDING_SERVER_SOCKET` is set; silent per-worker fallback is a regression unless the sidecar import/start is deliberately forced to fail by a test.
- The Docker image must build Headroom from `HEADROOM_GIT_REPO=https://github.com/stgmt/headroom.git` at pinned `HEADROOM_GIT_REF`, not only from the public PyPI wheel. Keep `HEADROOM_RUST_TOOLCHAIN=1.88.0`; Debian's older Rust has failed the fork build.
- Preserve runtime `.env` secrets and user OAuth/account state. Never regenerate or print secrets just to repair compose/docs. If `REDIS_PASSWORD` is empty, generate one, then recreate Redis and sub2api consistently without exposing the value.
- Keep all state on host bind mounts under `SUB2API_STATE_ROOT`: Headroom `/root/.headroom`, `/root/.cache/headroom`, `/root/.cache/huggingface`; sub2api `/app/data`; Postgres parent `/var/lib/postgresql`; Redis `/data`. Do not accept Docker named volumes for this profile.
- For `postgres:18-alpine`, bind the host state directory to `/var/lib/postgresql` and keep `PGDATA=/var/lib/postgresql/data`. Do not bind directly to `/var/lib/postgresql/data`; the image declares `/var/lib/postgresql` as a volume and the nested bind can make `initdb` loop on a non-empty directory.
- Use one autostart owner for the whole compose stack: `Sub2API Codex Proxy Stack Autostart`. Remove stale separate host `headroom-proxy` tasks or Startup-folder launchers.
- On Docker-in-WSL, Windows `127.0.0.1:8787` can hang even when Docker health is green. If WSL/Docker health works but Windows localhost hangs, publish Headroom on `0.0.0.0`, set Claude Code `ANTHROPIC_BASE_URL` to `http://<wsl-eth0-ip>:8787`, and keep direct sub2api `:18081` only as a diagnostic/admin bypass.
- Runtime proof beats source proof. A committed patch is not active until the running `headroom-sub2api` container proves it.
- Host `nvidia-smi` or Docker `gpus: all` alone does not make Kompress use CUDA. The image needs CUDA PyTorch, compose must set `HEADROOM_KOMPRESS_BACKEND=pytorch`, Docker inspect must show GPU `DeviceRequests`, and a live preload must return backend `pytorch` on device `cuda`. Keep CPU as the portable fallback.
- Treat a live-proven CUDA deployment as sticky. The sub2api GPU overlay must own `target: gpu`, `gpus: all`, `HEADROOM_KOMPRESS_BACKEND=pytorch`, `HEADROOM_FORCE_KOMPRESS=1`, and `HEADROOM_DISABLE_KOMPRESS=0`; setup/autostart may resolve `auto` to CUDA but must never silently downgrade persisted `cuda` after a transient WSL/NVIDIA probe failure. Do not start this profile with a bare base-compose launcher.
- A failed WSL `docker inspect` is not evidence of a CPU profile. The sub2api verifier must use the selected distro, retry bounded `Wsl/Service/0x8007274c` failures, and require a successful env inspection before it may skip CUDA proof.
- If Docker/WSL has no live CUDA proof, do not leave CPU ONNX Kompress enabled on Claude Code's hot path. Set `HEADROOM_FORCE_KOMPRESS=0` and `HEADROOM_DISABLE_KOMPRESS=1`, pass both through compose/generated `.env`, recreate Headroom, and prove `/health` reports `kompress.status=disabled` with no growing compression queue.
- Treat local keys that begin with `Bearer sk-sub2api` as Anthropic-compatible sub2api credentials, not OpenAI keys. A regression here sends `/v1/models` to OpenAI and produces fake-looking `Incorrect API key provided: sk-sub2a***` errors before sub2api can route GPT/Qwen/Claude/Fable.
- RTK remains command-side and must be installed in the same OS/user account that executes Claude Code Bash. Container-only RTK cannot rewrite host commands. For Windows Claude Code plus WSL Docker, use the paired `install-claude-rtk.ps1`, pinned Windows plus WSL RTK, and one `PreToolUse(Bash)` bridge with `MSYS2_ARG_CONV_EXCL='*'`. For Claude Code installed on a native Ubuntu host or Hyper-V VM outside its devcontainers, use `install-claude-rtk.sh` as that Claude user and one absolute native-Linux hook. Never install RTK only in a devcontainer when Claude runs on the outer Ubuntu host. Both topologies require RTK 0.42.4 and exclusions for `cat`/`git diff`/`git show`/`curl`.
- Bind host RTK state at `/root/.local/share/rtk` only when Headroom can access that same filesystem. In split-host VM -> remote Headroom layouts, prove VM-side savings from the VM's `rtk gain`; do not claim Headroom dashboard parity unless that history is explicitly shared.
- The loopback-only sub2api profile must not inherit Headroom's library default of 60 RPM. Every Claude window and subagent shares one API-key-plus-IP bucket, so that default turns healthy local fan-out into `429 {"detail":"Rate limited. Retry after ..."}` tool failures that Claude Code does not reliably replay. The paired profile defaults to `HEADROOM_RPM=6000` and `HEADROOM_TPM=100000000`; verify the effective values through `/stats` and run its invalid-key burst probe after recreating Headroom.
- The paired Claude Code profile must run `HEADROOM_OUTPUT_SHAPER=1` with `HEADROOM_EFFORT_ROUTER=0`. The shaper's effort router defaults on when omitted and rewrites explicit `output_config.effort=max` to `low` on clean `tool_result` continuations. Claude's transcript still labels those responses `max`; prove preservation with a pre-Headroom request-body tap plus matching sub2api `usage_logs.reasoning_effort`, not the UI alone.

## Required Checks

Run after Headroom source changes:

```powershell
python -m py_compile headroom/proxy/handlers/anthropic.py headroom/proxy/handlers/streaming.py headroom/memory/adapters/watchdog.py headroom/memory/factory.py
python -m pytest tests/test_stgmt_claude_code_recovery.py tests/test_cli_proxy_embedding_server.py tests/test_mid_turn_steering.py tests/test_anthropic_memory_mixed_tools.py
```

Run after Docker/runtime changes in `sub2api`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/docs/skills/sub2api-claude-code-codex/scripts/verify-claude-code-sub2api.ps1 -BaseUrl http://<wsl-ip>:8787 -SkipApiProbe -SkipClaudeProbe
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker ps --filter label=com.docker.compose.project=sub2api-codex --format "{{.Names}}|{{.Image}}|{{.Status}}"'
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker inspect headroom-sub2api sub2api-codex --format "{{.Name}} source={{index .Config.Labels \"org.opencontainers.image.source\"}} revision={{index .Config.Labels \"org.opencontainers.image.revision\"}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}"'
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker inspect headroom-sub2api --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -E "^HEADROOM_(OUTPUT_SHAPER=1|EFFORT_ROUTER=0)$"'
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker inspect headroom-sub2api sub2api-codex sub2api-codex-postgres sub2api-codex-redis --format "{{.Name}} {{range .Mounts}}{{.Destination}}={{.Type}} {{end}}"'
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api python -c "import torch; from headroom.transforms.kompress_compressor import KompressCompressor; print(torch.cuda.is_available(), torch.cuda.get_device_name(0), KompressCompressor().preload(allow_download=False))"
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api benchmark-headroom-kompress --require-cuda
rtk gain --format json
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api rtk gain --format json
wsl.exe -d Ubuntu-24.04 -- docker exec headroom-sub2api headroom perf --format json
node backend/docs/skills/sub2api-claude-code-codex/scripts/test-headroom-rate-limit-burst.mjs http://127.0.0.1:8787 96
```

Before any RTK claim, locate the actual Claude binary and choose the installer
for that host, not for the Docker location:

```text
Windows Claude + WSL Docker -> stgmt/sub2api .../scripts/install-claude-rtk.ps1
Native Ubuntu/Hyper-V Claude -> stgmt/sub2api .../scripts/install-claude-rtk.sh
Claude inside devcontainer   -> run install-claude-rtk.sh inside that container
```

For RTK, require a real fresh Claude Code Bash call whose debug log says
`Hook PreToolUse:Bash ... success` and `modified tool input keys`, plus an
increment in that Claude host's `rtk gain --format json`. Require matching
host/container totals only when Headroom mounts the same RTK state. A synthetic
hook probe proves rewrite syntax but does not prove automatic Claude use. On
Windows, a direct PowerShell-to-WSL probe also misses Git Bash path conversion
and is insufficient.

Expected runtime marker:
`WATCHDOG_RETRY_OK attempts=2 response=WATCHDOG_RETRY_RESPONSE`

Expected mixed-memory marker:
`Memory: Deferred 1 client-owned tool call(s) from continuation: ['Bash']`

For native compact routing, the installed handler must contain `_is_claude_code_compact_request`, `x-sub2api-claude-compact`, and `headroom:claude_code_compact_prompt_preserved`. A real forked Claude Code `/compact`, not only a synthetic probe, must produce that transform marker in `proxy-requests.jsonl` and a Spark/Luna compact route in sub2api `usage_logs`.

## Update Discipline

- Every new production gotcha gets an entry in `docs/stgmt-gotchas.md` with date, symptom, mechanism, fix, files, and proof.
- If a Headroom fix affects the local Claude Code stack, mirror the install/verify guidance into the `sub2api-claude-code-codex` skill.
- If a sub2api compose/profile fix affects Headroom behavior, mirror the invariant here too, sync the local Codex skill, and push both repos when both changed.
- When the Claude host topology changes, rerun the matching RTK installer and live proof. Do not infer host wiring from RTK being present in the Headroom image or a devcontainer.
- Keep `stgmt/headroom` and `stgmt/sub2api` commits linked in final reports when both repos change.
- Preserve field research as repo-owned docs, not only chat history or machine-local skills. Record primary sources, exact runtime proof, benchmark method, known limits, and the integration issue that owns remaining work.
- Keep `skills/headroom-sub2api-maintainer/references/gpu-kompress.md` byte-identical to `docs/stgmt-gpu-kompress.md` whenever the field report changes.
