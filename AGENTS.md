# OpenCode for Home Assistant

A Home Assistant custom integration (`custom_components/opencode`) that makes OpenCode agents into Assist conversation agents. It talks to an OpenCode 2 server's session API: each Assist conversation gets its own OpenCode session, and the answer streams back from the server's event stream.

## Layout

- `custom_components/opencode/api.py`: the HTTP and event-stream client for one OpenCode server
- `custom_components/opencode/config_flow.py`: one config entry per server, with a `conversation` subentry per agent and an `ai_task_data` subentry per AI task
- `custom_components/opencode/entity.py`: the base both entities share: creating sessions, streaming an answer, its timeout and fallbacks
- `custom_components/opencode/conversation.py`: the conversation entity, its session map and locks, and handing a slow answer off to announce later
- `custom_components/opencode/ai_task.py`: the AI task entity, one session per task, with structured output as JSON
- `custom_components/opencode/mcp.py`: serving Home Assistant's LLM APIs to OpenCode sessions as an MCP server over the client MCP socket
- `custom_components/opencode/speech.py`: cleaning Markdown answers into plain text for a voice satellite, a line at a time
- `tests/`: pytest with `pytest-homeassistant-custom-component`, against a fake client

## Stack

- Python 3.14 and uv, pinned in `mise.toml`. `pyproject.toml` pins the dev dependencies exactly.
- Ruff for linting and formatting. Follow Home Assistant Core's conventions: probatio schemas, translated exceptions, no `from __future__ import annotations`.
- The integration has no requirements of its own. It uses Home Assistant's aiohttp session.

## Checks

- `mise run check` runs Ruff and the tests. Run it before a push. `mise run format` writes fixes.
- `hk.pkl` is the pre-push hook: the shared Markdown, YAML, Actions and shell lints, plus Ruff. Install it once with `hk install`.
- To try it on a dev Core, symlink `custom_components/opencode` into the config's `custom_components/` and restart Core.

## Server behaviour

- Home Assistant pairs with a server from a one-time `opencode pair` link: it redeems the code at `/auth/connect/{code}` for a token and sends it as the Basic auth password. Paired tokens last 30 days, so the entry swaps its token for a new one every week, without reloading. A refused token starts reauth, which asks for a new link.
- Sessions deny the `question` permission when created. Other permission requests are rejected and forms cancelled, since nobody can answer them from a speaker.
- For agents with **Control Home Assistant** (`llm_hass_api`), the entry opens one `/api/experimental/client-mcp?name=homeassistant` WebSocket per directory and answers MCP JSON-RPC over it. OpenCode offers the tools to every session in that directory as `homeassistant_*`, so the agent's sessions allow `homeassistant_*` when created, and each call is answered with the API instance of its session's latest turn (from `_meta["ai.opencode/sessionID"]`). Calls from any other session are refused.
- A prompt to a session that's gone (404) or can't take it (409) starts a new session. If the event stream drops mid-turn, the entity waits on `/api/experimental/session/{id}/wait` and reads the last message instead.
- A turn still running at the Timeout is interrupted, unless a voice satellite asked: then the answer says so, and a background task waits on `/wait` and announces the last message with `assist_satellite.announce`.
- On a voice satellite, the first prompt asks for plain speech with anything bigger after a `[full answer]` line. The entity strips Markdown from what's spoken and, when it leaves something out, saves the answer as written as a persistent notification.
- Sessions are archived, not deleted, when Assist forgets the conversation.

## Rollout

- Changes go straight to `main`, with a short imperative commit subject. Renovate opens dependency updates and automerges the ones that pass.
- `tooling.yml` records the OpenCode and Core releases the integration was last reviewed against. Upkeep's drift job hands a newer one to an agent with the review note there; set `reviewed` once it's reviewed.
- CI runs the mise `lint` and `test` tasks through the shared workflows in `timmo001/workflows`, pinned to a tag, plus HACS and hassfest validation.
- A release is a published GitHub release. Bump `version` in `manifest.json` and `pyproject.toml` together.
- The README's install steps are what Marketplace users follow, so keep them accurate when install changes.
