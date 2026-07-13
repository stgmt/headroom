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

## Critical Invariants

- Claude Code must not receive Headroom's private HTTP 202 `headroom_queued` response for streaming `/v1/messages`.
- Claude Code stream keys must include both `x-claude-code-session-id` and `x-claude-code-agent-id` unless an explicit `x-headroom-session-id` is provided.
- Handler watchdog timeout must cancel the primary handler and retry once through Headroom bypass/passthrough before any 504 leaves the proxy.
- `--embedding-server` must use `headroom.memory.adapters.watchdog.SocketEmbedderClient` when `HEADROOM_EMBEDDING_SERVER_SOCKET` is set; silent per-worker fallback is a regression unless the sidecar import/start is deliberately forced to fail by a test.
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
```

Expected runtime marker:
`WATCHDOG_RETRY_OK attempts=2 response=WATCHDOG_RETRY_RESPONSE`

## Update Discipline

- Every new production gotcha gets an entry in `docs/stgmt-gotchas.md` with date, symptom, mechanism, fix, files, and proof.
- If a Headroom fix affects the local Claude Code stack, mirror the install/verify guidance into the `sub2api-claude-code-codex` skill.
- Keep `stgmt/headroom` and `stgmt/sub2api` commits linked in final reports when both repos change.
