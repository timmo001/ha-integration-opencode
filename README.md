# OpenCode for Home Assistant

A custom Home Assistant integration that lets your [OpenCode](https://opencode.ai) agents answer Assist, from the chat dialog or a voice satellite. Each OpenCode server is one entry, and each agent you add to it is its own conversation agent, with its own OpenCode agent, model and instructions.

> [!WARNING]
> This integration is experimental, breaking changes may occur. No support will be given

It needs an OpenCode 2 server Home Assistant can reach, such as the [OpenCode app](https://github.com/timmo001/app-opencode) or OpenCode on another machine. On another machine, have OpenCode listen on the network with `opencode service set hostname 0.0.0.0`, then `opencode service restart`, and allow port 49374 through its firewall.

> [!TIP]
> On Home Assistant OS, the [OpenCode app](https://github.com/timmo001/app-opencode) runs OpenCode on the same machine, opened from the sidebar. This integration finds the app by itself, and the app pairs Home Assistant for you.

This is a community project. It isn't made or endorsed by Anomaly, the makers of OpenCode, or by Home Assistant.

<!-- ha-marketplace:exclude-start -->

## Installation

### Marketplace (Recommended)

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=timmo001&repository=ha-integration-opencode&category=integration)

1. Open the repository in the Marketplace with the button above
2. Select **Install**
3. Restart Home Assistant

### HACS

Since this integration is not yet in the default HACS store, you need to add it as a custom repository:

1. Open HACS in your Home Assistant instance
2. Click the **3 dots** in the top right corner
3. Select **"Custom repositories"**
4. Add repository URL: `https://github.com/timmo001/ha-integration-opencode`
5. Select category: **Integration**
6. Click **"ADD"**
7. Find "OpenCode" in the list and click **Download**
8. Restart Home Assistant

### Manual

1. Copy `custom_components/opencode` into your `config/custom_components` folder
2. Restart Home Assistant

<!-- ha-marketplace:exclude-end -->

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=opencode)

With the [OpenCode app](https://github.com/timmo001/app-opencode) on Home Assistant OS, Home Assistant finds it by itself: **Settings** > **Devices & services** shows OpenCode as discovered. Add it, and the app pairs Home Assistant for you, so skip to step 3. For any other OpenCode server:

1. Use the button above, or go to **Settings** > **Devices & services** > **Add integration** and pick **OpenCode**
2. On the server, run `opencode pair`, or select **Show a pairing code** on the **Connect** tab of the [OpenCode app](https://github.com/timmo001/app-opencode)'s sidebar page, and paste one of its links, such as `http://192.168.1.10:49374/auth/connect/…`. Links work once and expire in 5 minutes. Home Assistant keeps the token the link gives it and renews it every week, so there's no password to enter
3. Pick the model that answers. A small one is picked for you: the cheapest paid model on the server that can use tools, since quick answers matter more than depth for most spoken questions. Clear it to use the agent's own default
4. A conversation agent and an AI task are added with the server's `build` agent and that model. Each is named after its model and server, such as "MAI Code 2 Flash (arch-station)" and "MAI Code 2 Flash AI task (arch-station)", so agents on different servers or models are easy to tell apart. A server you paired is named by its hostname. The app's entry keeps its own name, and its agents end in "(app)", such as "MAI Code 2 Flash (app)". Use **Reconfigure** on either to pick another agent, model or variant, and a name you haven't changed follows the new model. Use **Add conversation agent** and **Add AI task** for more on the same server, with no need to pair again
5. Go to **Settings** > **Voice assistants** and pick the agent as an assistant's conversation agent

   [![Open your Home Assistant instance and show your voice assistants.](https://my.home-assistant.io/badges/voice_assistants.svg)](https://my.home-assistant.io/redirect/voice_assistants/)

Add a second server, such as a more powerful machine, as another entry, and use its agents for the assistants that need them.

## How it works

- Each Assist conversation is one OpenCode session, so follow-up questions keep their context. The session is archived on the server when Assist forgets the conversation, after five minutes.
- The answer streams back as the agent writes it, so a voice satellite can start speaking early.
- Asked on a voice satellite, an answer that takes longer than the agent's Timeout doesn't hold up the satellite. It says "On it, I'll tell you when it's done", and announces the answer once OpenCode finishes, up to 10 minutes later. Typed in Assist, OpenCode is stopped at the Timeout instead.
- On a voice satellite, the agent is told it's being heard, and to keep anything bigger than a few sentences, such as code, a table or a document, after the spoken answer. What the speaker reads is plain text: Markdown is stripped whatever the model writes. When something is left out, the answer as written is saved as a notification, and the speaker says so. Typed in Assist, the answer shows as written.
- Nobody can approve anything from a speaker, so the session can't ask questions, and any permission it asks for is refused with a note saying why. Home Assistant's own tools are the exception: they're allowed without asking.
- What OpenCode thinks and every tool it calls, with the result, are added to the conversation, so they show in the Assist dialog and in each voice assistant's debug view, as they do for Home Assistant's own AI integrations. They're shown, not spoken.

### Home Assistant's tools

An agent with **Control Home Assistant** set can see and control your home with the same tools Home Assistant's own AI integrations use, limited to the entities you expose to Assist. New agents have **Device control** (the Assist API) picked; an agent added before needs **Reconfigure** to turn it on. The **Home Assistant** API only works for administrators, so voice satellites can't use it. Home Assistant hands the tools to OpenCode over a connection of its own, so there's nothing to set up on the server. It needs OpenCode 2.3.2 or later.

- The agent keeps its own tools and MCP servers alongside them, such as one with your home's history, and is told to use Home Assistant's first. With OpenCode's Code Mode, they're under `tools.homeassistant`.
- Tool calls act with the same context as the conversation, so "turn on the lights" from a satellite means the lights in its area.
- OpenCode lists the tools, as `homeassistant_*`, in every session in the agent's Directory on the server, not only Home Assistant's. Only sessions Home Assistant started can use them: anything else that calls one is refused. To keep them out of your own sessions' tool lists, give the agent a Directory you don't work in.

To use Home Assistant's tools from your own OpenCode sessions as well, add Home Assistant's [Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/):

1. Add the **Model Context Protocol Server** integration in Home Assistant

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mcp_server)

2. On the OpenCode server, add Home Assistant as an MCP server, with the address OpenCode reaches Home Assistant on:

   ```sh
   opencode mcp add home-assistant --global --url http://homeassistant.local:8123/api/mcp
   ```

3. Open OpenCode, run `/mcps`, pick `home-assistant` and sign in to Home Assistant in the browser

### AI tasks

An AI task lets automations and scripts hand OpenCode work with the **AI Task: Generate data** action (`ai_task.generate_data`). Each task runs in a session of its own, archived when it's done. Give the action a structure and OpenCode answers with data in that shape, such as a list of rooms. AI tasks have the same options as agents, without Instructions, and a Timeout of 10 minutes by default.

## Options

| Option                 | What it does                                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Agent                  | The OpenCode agent that answers                                                                                                      |
| Model                  | The model the session uses. Empty uses the agent's default                                                                           |
| Variant                | The model's effort variant, such as `high`. Empty uses the default                                                                   |
| Directory              | The folder on the server the session works in. Empty uses the server's default, usually the home folder of the user OpenCode runs as |
| Instructions           | Sent at the start of each conversation. A template, like other Assist agents                                                         |
| Control Home Assistant | Home Assistant's tools the agent can use, such as Device control. Agents only                                                        |
| Timeout                | How long to wait for an answer. On a voice satellite, a slower one is announced when it's done                                       |
