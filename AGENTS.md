# OpenCode for Home Assistant

A Home Assistant custom integration (`custom_components/opencode`) that makes OpenCode agents into Assist conversation agents. It talks to an OpenCode 2 server's session API: each Assist conversation gets its own OpenCode session, and the answer streams back from the server's event stream.

## Layout

- `custom_components/opencode/api.py`: the HTTP and event-stream client for one OpenCode server
- `custom_components/opencode/config_flow.py`: one config entry per server, with a `conversation` subentry per agent
- `custom_components/opencode/conversation.py`: the conversation entity, its session map, locks and fallbacks
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
- A prompt to a session that's gone (404) or can't take it (409) starts a new session. If the event stream drops mid-turn, the entity waits on `/api/experimental/session/{id}/wait` and reads the last message instead.
- Sessions are archived, not deleted, when Assist forgets the conversation.

## Rollout

- Changes go straight to `main`, with a short imperative commit subject. Renovate opens dependency updates and automerges the ones that pass.
- CI runs the mise `lint` and `test` tasks through the shared workflows in `timmo001/workflows`, pinned to a tag, plus HACS and hassfest validation.
- A release is a published GitHub release. Bump `version` in `manifest.json` and `pyproject.toml` together.
- The README's install steps are what HACS users follow, so keep them accurate when install changes.
