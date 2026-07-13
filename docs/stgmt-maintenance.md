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
