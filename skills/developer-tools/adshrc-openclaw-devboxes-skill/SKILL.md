---
name: devboxes
description: Manage development environment containers (devboxes) with web-accessible VSCode, VNC, and app routing via Traefik or Cloudflare Tunnels. Use when the user asks to create, start, stop, list, or manage devboxes/dev environments, spin up a development container, set up a coding sandbox, or configure the devbox infrastructure for the first time (onboarding).
---

# Devbox Skill

Devboxes are OpenClaw sandbox containers running a custom image with VSCode Web, noVNC, Chromium (CDP), and up to 5 app ports routed via **Traefik** or **Cloudflare Tunnels**.

OpenClaw manages the full container lifecycle. The **main agent** assigns each devbox a sequential ID by maintaining a local counter file, then passes `DEVBOX_ID` to the subagent task. The devbox agent runs `devbox-init` as its first action, which builds URL env vars, writes env files, and sets up routing.

## File Locations

Resolve paths relative to this SKILL.md's parent directory.

Key files:

- `references/setup-script-guide.md` — conventions for project setup scripts (`.openclaw/setup.sh`)

## Architecture

- **Agent id:** `devbox` (configured in openclaw.json)
- **Sandbox mode:** `all` / `scope: session` — one container per session
- **Image:** `ghcr.io/adshrc/openclaw-devbox:latest` (pulled from GHCR)
- **Network:** `traefik` (for Traefik routing) or default Docker network (for Cloudflare Tunnel routing)
- **Browser:** `sandbox.browser.enabled: true`, CDP on port 9222

### ID Assignment

The **main agent** manages the devbox counter:

1. Reads and increments `/home/node/.openclaw/.devbox-counter` before spawning
2. Includes the `DEVBOX_ID` in the task, instructing the devbox agent to run `devbox-init {id}` as its very first action

### Two-Phase Startup

**Phase 1 — Entrypoint** (runs automatically at container start):

- Starts core services: Xvfb, Chromium (CDP), VNC, VSCode Web
- Does NOT require `DEVBOX_ID` — services are available immediately

**Phase 2 — `devbox-init <id>`** (run by the sub-agent/devbox agent):

1. Builds `APP_URL_1..5`, `VSCODE_URL`, `NOVNC_URL` from tags + domain + ID
2. Writes `/etc/devbox.env` and `/etc/profile.d/devbox.sh` (available in all shells)
3. Routes based on `ROUTING_MODE`:
   - **`traefik`** (default): Writes Traefik config to `/traefik/devbox-{id}.yml`
   - **`cloudflared`**: Generates cloudflared ingress config, registers DNS CNAME records via CF API, starts `cloudflared tunnel run`

The `devbox-init` script is installed at `/usr/local/bin/devbox-init` in the Docker image.

### Bind Mounts (configured in openclaw.json)

| Agent path                     | Devbox container path | Purpose                                       |
| ------------------------------ | --------------------- | --------------------------------------------- |
| `/home/node/.openclaw/traefik` | `/traefik`            | Route configs (only if using Traefik routing) |

**Important:** Due to OpenClaw Security measures, all user capabilites are dropped by default. So even root (in the devbox) has no write access to bind mounts, and can only read from them. The only solution currently is `chmod 777` on the host path that is mapped to `/home/node/.openclaw/traefik`.

### Known Paths

These paths are always the same inside the OpenClaw container:

- **OpenClaw data:** `/home/node/.openclaw`
- **Traefik dynamic config:** `/home/node/.openclaw/traefik` (must be mounted into the OpenClaw container; only if using Traefik routing)

## Onboarding Flow

**Important:** After this skill is installed, the next step MUST be this onboarding flow. The user CANNOT use the devboxes skill until the onboarding flow is completed and the infrastructure is set up.

**Important:** This runs on the MAIN agent, NOT in a sandbox. The main agent has access to `exec`, `gateway`, and the file system.

When the user asks to set up the devbox skill, do the following:

### Step 1: Verify Docker Access

Verify that you have access to the Docker socket and Docker binary:

```bash
which docker
docker version
```

If not, abort here and tell the user they need to run the OpenClaw container with:

```
-v /usr/bin/docker:/usr/bin/docker:ro
-v /var/run/docker.sock:/var/run/docker.sock
```

and that they need to set `chmod 666 /var/run/docker.sock` manually on the host, so that the OpenClaw container can work with it.

The OpenClaw container needs to be restarted then. After that, they can ask to set up the devbox skill again.

### Step 2: Gather configuration info

Ask the user for:

- **Routing mode**: Traefik or Cloudflare Tunnel?
- **Domain**: with wildcard A-Record pointing to the server (e.g. `*.example.com`)
- **GitHub token** (optional): for cloning private repos inside devboxes

If **Cloudflare Tunnel** is chosen, also ask for:

- **Cloudflare API token**: must have permissions for the zone (DNS edit + Tunnel edit)

### Step 3: Validate the Host Mapping Path (ONLY if using Traefik routing, skip otherwise!)

Find out what the host path/mapping is for `/home/node/.openclaw` inside the container:

```bash
# Returns the host path that is mapped to /home/node/.openclaw inside the container
docker inspect --format='{{range .Mounts}}{{if eq .Destination "/home/node/.openclaw"}}{{.Source}}{{end}}{{end}}' $(hostname)
```

Store the value as `HOST_OPENCLAW_PATH`. If `HOST_OPENCLAW_PATH` is a "system directory", OpenClaw will not be able to spawn a devbox.

System directories are: /etc, /private/etc, /proc, /sys, /dev, /root, /boot, /run, /var/run, /private/var/run, /var/run/docker.sock, /private/var/run/docker.sock and /run/docker.sock.

If the `HOST_OPENCLAW_PATH` is such a "system directory", abort here and tell the user they need to change their OpenClaw container setup to use a host path for OpenClaw data that is not a system directory. For example, they can create a directory like `/home/openclaw` or `/opt/openclaw` on the host.

### Step 4: Verify Routing prerequisites

#### If routing mode is Traefik:

Check that /home/node/.openclaw/traefik is mounted:

```bash
ls /home/node/.openclaw/traefik
```

If `/home/node/.openclaw/traefik` doesn't exist, abort here and tell the user they need to add e.g. `-v path_to_traefik:/home/node/.openclaw/traefik` to their OpenClaw container and restart it. Remind the user that they cannot use system directories for the host path (OpenClaw Sandboxes restriction). After that, a container restart is needed, and then they can ask to set up the devbox skill again.

#### If routing mode is Cloudflare Tunnel:

Validate the CF API token and domain:

```bash
# 1. Get zone ID for the domain (extract root domain from the provided domain)
curl -s -X GET "https://api.cloudflare.com/client/v4/zones?name=${ROOT_DOMAIN}" \
  -H "Authorization: Bearer ${CF_API_AUTH}" | jq .

# 2. Get account ID from the zone response
# account_id = .result[0].account.id
# zone_id = .result[0].id
```

Then create the tunnel:

```bash
# 4. Create a named tunnel
curl -s -X POST "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/cfd_tunnel" \
  -H "Authorization: Bearer ${CF_API_AUTH}" \
  -H "Content-Type: application/json" \
  -d '{"name": "devboxes@'"$(hostname)"'", "config_src": "local", "tunnel_secret": "'$(openssl rand -base64 32)'"}' | jq .

# Extract tunnel_id and tunnel_token from the response
# tunnel_id = .result.id
# tunnel_token = .result.token
```

Store the values: `CF_API_AUTH`, `CF_ZONE_ID`, `CF_ACCOUNT_ID`, `CF_TUNNEL_ID`, `CF_TUNNEL_AUTH`.

### Step 5: Create counter file

```bash
echo "0" > /home/node/.openclaw/.devbox-counter
```

**Important:** The counter file is managed by the main agent only. The counter CANNOT be reset, or decremented, only increment is allowed!

### Step 6: Configure OpenClaw

First, check the current agents config:

```bash
node /app/openclaw.mjs config get agents
```

Then, decide what needs to be adjusted based on the existing config:

#### Main Agent

If there is an agent with `default: true`, note its index and add `subagents.allowAgents` to it:

```bash
node /app/openclaw.mjs config set agents.list[{index}].subagents.allowAgents '["devbox"]' --json
```

then (if needed), set sandbox mode to `off` (since the main agent doesn't need a sandbox):

```bash
node /app/openclaw.mjs config set agents.list[{index}].sandbox.mode "off"
```

else, if there's no agent with `default: true`, create one at the next index with the necessary structure:

```bash
node /app/openclaw.mjs config set agents.list[{index}] '{
  "id": "main",
  "default": true,
  "subagents": {
    "allowAgents": [
        "devbox"
    ]
  },
  "sandbox": {
    "mode": "off"
  }
}' --json
```

#### Devbox Agent

1. Pull the Docker image so that it's available when OpenClaw tries to spawn it:

```bash
docker pull ghcr.io/adshrc/openclaw-devbox:latest
```

2. Add the devbox agent at the next index in agents.list. Adjust the sandbox and docker config as needed (replace placeholders in curly brackets):

```bash
node /app/openclaw.mjs config set agents.list[{index}] '{
    "id": "devbox",
    "name": "Devbox Agent",
    "tools": {
      "exec": {
        "host": "sandbox"
      }
    },
    "sandbox": {
      "mode": "all",
      "workspaceAccess": "rw",
      "scope": "session",
      "docker": {
        "image": "ghcr.io/adshrc/openclaw-devbox:latest",
        "readOnlyRoot": false,
        "user": "0:0",
        "network": "traefik", # Only needed for Traefik routing mode, exclude otherwise
        "env": {
          "ENABLE_VNC": "true",
          "ENABLE_VSCODE": "true",
          "DEVBOX_DOMAIN": "{domain}",
          "APP_TAG_1": "app1",
          "APP_TAG_2": "app2",
          "APP_TAG_3": "app3",
          "APP_TAG_4": "app4",
          "APP_TAG_5": "app5",
          "GH_AUTH": "{github_token}",
          "ROUTING_MODE": "{traefik|cloudflared}",
          # Cloudflare Tunnel variables (only needed if using cloudflared routing, exclude otherwise)
          "CF_TUNNEL_AUTH": "{cf_tunnel_token}",
          "CF_API_AUTH": "{cf_api_token}",
          "CF_ZONE_ID": "{cf_zone_id}",
          "CF_TUNNEL_ID": "{cf_tunnel_id}",
        },
        "binds": [
          "{host_openclaw_path}/traefik:/traefik:rw" # Only needed for Traefik routing mode, exclude otherwise
        ]
      },
      "browser": {
        "enabled": true,
        "cdpPort": 9222
      },
      "prune": {
        "idleHours": 0,
        "maxAgeDays": 0
      }
    }
  }
]' --json
```

3. Allow Agents to communicate with each other so that subsequent tasks can be sent to the devbox agent:

```bash
node /app/openclaw.mjs config set tools.agentToAgent.enabled true
node /app/openclaw.mjs config set tools.sessions.visibility "all"
```

After this is done, restart the gateway to apply the changes. If this is not working (e.g. command is disabled), ask the user to restart the OpenClaw container manually.

## Workflow: Spawn a Devbox

### Step 1: Assign ID (main agent)

Read and increment the counter file:

```bash
DEVBOX_ID=$(( $(cat /home/node/.openclaw/.devbox-counter) + 1 ))
echo "$DEVBOX_ID" > /home/node/.openclaw/.devbox-counter
```

### Step 2: Spawn subagent (main agent)

Use `devbox-<id>` as the label and include the `DEVBOX_ID` in the task:

```python
sessions_spawn(
    agentId="devbox",
    label=f"devbox-{DEVBOX_ID}",
    task=f"Your task description. Your DEVBOX_ID is {DEVBOX_ID} — run `devbox-init {DEVBOX_ID}` as your very first action before doing anything else. This sets up routing and writes env vars. After that, env vars (APP_URL_*, VSCODE_URL, etc.) are in your shell via `source /etc/profile.d/devbox.sh`. GitHub token is in $GH_AUTH. ALWAYS use /workspace as the working directory! When cloning, the structure must be /workspace/<repo>."
)
```

### Step 3: Report URLs to user (main agent)

The main agent already knows the assigned ID from Step 1. Report:

- VSCode: `https://vscode-{DEVBOX_ID}.{domain}`
- noVNC: `https://novnc-{DEVBOX_ID}.{domain}/vnc.html`
- App URLs: `https://{tag}-{DEVBOX_ID}.{domain}`

### Cleanup

OpenClaw manages container lifecycle — containers are removed when sessions end. Traefik route configs left behind are harmless.

## Environment Variables

### Static (set in openclaw.json sandbox.docker.env)

| Variable          | Example                    | Description                                          |
| ----------------- | -------------------------- | ---------------------------------------------------- |
| `ROUTING_MODE`    | `traefik` or `cloudflared` | Routing backend (default: `traefik`)                 |
| `GH_AUTH`         | `ghp_...`                  | GitHub PAT for cloning                               |
| `DEVBOX_DOMAIN`   | `example.com`              | Base domain                                          |
| `APP_TAG_1..5`    | `app1`, `app2`, ...        | Route tags                                           |
| `ENABLE_VNC`      | `true`                     | Enable noVNC                                         |
| `ENABLE_VSCODE`   | `true`                     | Enable VSCode Web                                    |
| `CF_TUNNEL_AUTH`  | `eyJ...`                   | Cloudflare tunnel run token (cloudflared only)       |
| `CF_API_AUTH`     | `abc123`                   | CF API token for DNS registration (cloudflared only) |
| `CF_ZONE_ID`      | `xyz789`                   | CF zone ID for the domain (cloudflared only)         |
| `CF_TUNNEL_ID`    | `uuid`                     | CF tunnel ID for CNAME targets (cloudflared only)    |

### Dynamic (built by `devbox-init`, available in all shells after running it)

| Variable        | Example                                | Description                 |
| --------------- | -------------------------------------- | --------------------------- |
| `DEVBOX_ID`     | `1`                                    | Auto-assigned sequential ID |
| `APP_URL_1..5`  | `https://app1-1.example.com`           | Full URLs per app slot      |
| `APP_PORT_1..5` | `8003..8007`                           | Internal ports              |
| `VSCODE_URL`    | `https://vscode-1.example.com`         | VSCode Web URL              |
| `NOVNC_URL`     | `https://novnc-1.example.com/vnc.html` | noVNC URL                   |

### Ports

| Port      | Service                        |
| --------- | ------------------------------ |
| 8000      | VSCode Web                     |
| 8002      | noVNC                          |
| 9222      | Chrome DevTools Protocol (CDP) |
| 8003-8007 | App slots 1-5                  |

## Browser

The devbox agent has browser access via Chromium CDP (port 9222). The subagent can use the `browser` tool to navigate, screenshot, and interact with apps running inside the container (use `http://localhost:{port}`).

## Project Setup Scripts

Projects can include `.openclaw/setup.sh` that runs inside the devbox. It has access to all env vars (`APP_URL_*`, `APP_PORT_*`, `DEVBOX_ID`, etc.) via `/etc/profile.d/devbox.sh`.

See `references/setup-script-guide.md` for conventions.
