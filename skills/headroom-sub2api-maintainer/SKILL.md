---
name: headroom-sub2api-maintainer
description: Maintain the stgmt/headroom fork for Claude Code through Headroom + sub2api + Codex/OpenAI subscription routing. Use for Headroom fork syncs, Claude Code stream hangs, Headroom private 202 queue responses, handler watchdog retry, embedding-server sidecar, WSL/Docker localhost relay issues, and keeping gotchas/tests/docs aligned with the sub2api Docker profile.
---

# Headroom sub2api Maintainer

Use this skill when work touches `stgmt/headroom`, the `headroom-sub2api` Docker image, or Claude Code routed through Headroom.

## Sources Of Truth

- Fork: `https://github.com/stgmt/headroom`
- Upstream: `https://github.com/headroomlabs-ai/headroom`
- Gotchas ledger: `docs/stgmt-gotchas.md`
- Maintenance guide: `docs/stgmt-maintenance.md`
- sub2api profile mirror: `stgmt/sub2api`, `deploy/claude-code-codex-headroom`, and `backend/docs/skills/sub2api-claude-code-codex`
- Local runtime profile on this machine: `C:\Users\stigm\Documents\Codex\2026-07-07\new-chat\work\sub2api-runtime`

## Critical Invariants

- Claude Code must not receive Headroom's private HTTP 202 `headroom_queued` response for streaming `/v1/messages`.
- Claude Code stream keys must include both `x-claude-code-session-id` and `x-claude-code-agent-id` unless an explicit `x-headroom-session-id` is provided.
- Handler watchdog timeout must cancel the primary handler and retry once through Headroom bypass/passthrough before any 504 leaves the proxy.
- `--embedding-server` must use `headroom.memory.adapters.watchdog.SocketEmbedderClient` when `HEADROOM_EMBEDDING_SERVER_SOCKET` is set; silent per-worker fallback is a regression unless the sidecar import/start is deliberately forced to fail by a test.
- The Docker image must build Headroom from `HEADROOM_GIT_REPO=https://github.com/stgmt/headroom.git` at pinned `HEADROOM_GIT_REF`, not only from the public PyPI wheel. Keep `HEADROOM_RUST_TOOLCHAIN=1.88.0`; Debian's older Rust has failed the fork build.
- Preserve runtime `.env` secrets and user OAuth/account state. Never regenerate or print secrets just to repair compose/docs. If `REDIS_PASSWORD` is empty, generate one, then recreate Redis and sub2api consistently without exposing the value.
- Keep all state on host bind mounts under `SUB2API_STATE_ROOT`: Headroom `/root/.headroom`, `/root/.cache/headroom`, `/root/.cache/huggingface`; sub2api `/app/data`; Postgres parent `/var/lib/postgresql`; Redis `/data`. Do not accept Docker named volumes for this profile.
- For `postgres:18-alpine`, bind the host state directory to `/var/lib/postgresql` and keep `PGDATA=/var/lib/postgresql/data`. Do not bind directly to `/var/lib/postgresql/data`; the image declares `/var/lib/postgresql` as a volume and the nested bind can make `initdb` loop on a non-empty directory.
- Use one autostart owner for the whole compose stack: `Sub2API Codex Proxy Stack Autostart`. Remove stale separate host `headroom-proxy` tasks or Startup-folder launchers.
- On Docker-in-WSL, Windows `127.0.0.1:8787` can hang even when Docker health is green. If WSL/Docker health works but Windows localhost hangs, publish Headroom on `0.0.0.0`, set Claude Code `ANTHROPIC_BASE_URL` to `http://<wsl-eth0-ip>:8787`, and keep direct sub2api `:18081` only as a diagnostic/admin bypass.
- Runtime proof beats source proof. A committed patch is not active until the running `headroom-sub2api` container proves it.

## Required Checks

Run after Headroom source changes:

```powershell
python -m py_compile headroom/proxy/handlers/anthropic.py headroom/proxy/handlers/streaming.py headroom/memory/adapters/watchdog.py headroom/memory/factory.py
python -m pytest tests/test_stgmt_claude_code_recovery.py tests/test_cli_proxy_embedding_server.py tests/test_mid_turn_steering.py
```

Run after Docker/runtime changes in `sub2api`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/docs/skills/sub2api-claude-code-codex/scripts/verify-claude-code-sub2api.ps1 -BaseUrl http://<wsl-ip>:8787 -SkipApiProbe -SkipClaudeProbe
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker ps --filter label=com.docker.compose.project=sub2api-codex --format "{{.Names}}|{{.Image}}|{{.Status}}"'
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker inspect headroom-sub2api sub2api-codex --format "{{.Name}} source={{index .Config.Labels \"org.opencontainers.image.source\"}} revision={{index .Config.Labels \"org.opencontainers.image.revision\"}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}"'
wsl.exe -d Ubuntu-24.04 -- bash -lc 'docker inspect headroom-sub2api sub2api-codex sub2api-codex-postgres sub2api-codex-redis --format "{{.Name}} {{range .Mounts}}{{.Destination}}={{.Type}} {{end}}"'
```

Expected runtime marker:
`WATCHDOG_RETRY_OK attempts=2 response=WATCHDOG_RETRY_RESPONSE`

## Update Discipline

- Every new production gotcha gets an entry in `docs/stgmt-gotchas.md` with date, symptom, mechanism, fix, files, and proof.
- If a Headroom fix affects the local Claude Code stack, mirror the install/verify guidance into the `sub2api-claude-code-codex` skill.
- If a sub2api compose/profile fix affects Headroom behavior, mirror the invariant here too, sync the local Codex skill, and push both repos when both changed.
- Keep `stgmt/headroom` and `stgmt/sub2api` commits linked in final reports when both repos change.
