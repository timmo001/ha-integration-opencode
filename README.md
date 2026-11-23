# OpenCode for Home Assistant

A custom Home Assistant integration that lets your [OpenCode](https://opencode.ai) agents answer Assist, from the chat dialog or a voice satellite. Each OpenCode server is one entry, and each agent you add to it is its own conversation agent, with its own OpenCode agent, model and instructions.

> [!WARNING]
> This integration is experimental, breaking changes may occur. No support will be given

It needs an OpenCode 2 server Home Assistant can reach, such as the OpenCode app or OpenCode on another machine. On another machine, have OpenCode listen on the network with `opencode service set hostname 0.0.0.0`, then `opencode service restart`, and allow port 49374 through its firewall.

This is a community project. It isn't made or endorsed by Anomaly, the makers of OpenCode, or by Home Assistant.

<!-- ha-marketplace:exclude-start -->

## Installation

### HACS (Recommended)

Since this integration is not yet in the default HACS store, you need to add it as a custom repository:

1. Open HACS in your Home Assistant instance
2. Click the **3 dots** in the top right corner
3. Select **"Custom repositories"**
4. Add repository URL: `https://github.com/timmo001/ha-opencode`
5. Select category: **Integration**
6. Click **"ADD"**
7. Find "OpenCode" in the list and click **Download**
8. Restart Home Assistant

### Manual

1. Copy `custom_components/opencode` into your `config/custom_components` folder
2. Restart Home Assistant

<!-- ha-marketplace:exclude-end -->

## Setup

With the [OpenCode app](https://github.com/timmo001/app-opencode) on Home Assistant OS, Home Assistant finds it by itself: **Settings** > **Devices & services** shows OpenCode as discovered. Add it, and the app pairs Home Assistant for you, so skip to step 3. For any other OpenCode server:

1. Go to **Settings** > **Devices & services** > **Add integration** and pick **OpenCode**
2. On the server, run `opencode pair`, or press **Pair a device** in the OpenCode app's sidebar, and paste one of its links, such as `http://192.168.1.10:49374/auth/connect/…`. Links work once and expire in 5 minutes. Home Assistant keeps the token the link gives it and renews it every week, so there's no password to enter
3. Pick the model that answers. A small one is picked for you: the cheapest paid model on the server that can use tools, since quick answers matter more than depth for most spoken questions. Clear it to use the agent's own default
4. An agent called "OpenCode conversation" and an AI task called "OpenCode AI Task" are added with the server's `build` agent and that model. Use **Reconfigure** on either to pick another agent, model or variant, or **Add conversation agent** and **Add AI task** for more
5. Go to **Settings** > **Voice assistants** and pick the agent as an assistant's conversation agent

Add a second server, such as a more powerful machine, as another entry, and use its agents for the assistants that need them.

## How it works

- Each Assist conversation is one OpenCode session, so follow-up questions keep their context. The session is archived on the server when Assist forgets the conversation, after five minutes.
- The answer streams back as the agent writes it, so a voice satellite can start speaking early.
- Asked on a voice satellite, an answer that takes longer than the agent's Timeout doesn't hold up the satellite. It says "On it, I'll tell you when it's done", and announces the answer once OpenCode finishes, up to 10 minutes later. Typed in Assist, OpenCode is stopped at the Timeout instead.
- On a voice satellite, the agent is told it's being heard, and to keep anything bigger than a few sentences, such as code, a table or a document, after the spoken answer. What the speaker reads is plain text: Markdown is stripped whatever the model writes. When something is left out, the answer as written is saved as a notification, and the speaker says so. Typed in Assist, the answer shows as written.
- Nobody can approve anything from a speaker, so the session can't ask questions, and any permission it asks for is refused with a note saying why.
- The OpenCode agent uses its own tools and MCP servers, not Home Assistant's Assist API. To control your home, give the agent a Home Assistant MCP server.

### AI tasks

An AI task lets automations and scripts hand OpenCode work with the **AI Task: Generate data** action (`ai_task.generate_data`). Each task runs in a session of its own, archived when it's done. Give the action a structure and OpenCode answers with data in that shape, such as a list of rooms. AI tasks have the same options as agents, without Instructions, and a Timeout of 10 minutes by default.

## Options

| Option       | What it does                                                                                    |
| ------------ | ----------------------------------------------------------------------------------------------- |
| Agent        | The OpenCode agent that answers                                                                 |
| Model        | The model the session uses. Empty uses the agent's default                                      |
| Variant      | The model's effort variant, such as `high`. Empty uses the default                              |
| Directory    | The folder on the server the session works in. Empty uses the server's default                  |
| Instructions | Sent at the start of each conversation. A template, like other Assist agents                    |
| Timeout      | How long to wait for an answer. On a voice satellite, a slower one is announced when it's done  |
