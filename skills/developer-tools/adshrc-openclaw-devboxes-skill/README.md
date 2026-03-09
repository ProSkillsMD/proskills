# Devboxes Skill for OpenClaw

Spin up a full development environment in seconds — just by asking your agent.

Each devbox is an isolated container with **VSCode in your browser**, a **visual desktop via VNC**, **headless Chromium** for testing, and **up to 5 routable app ports** — all accessible via clean URLs on your domain. No SSH tunnels, no port forwarding, no "works on my machine".

Need to prototype something? Clone a repo and start coding? Debug a frontend on a real browser? Spin up a devbox. It self-registers with Traefik, assigns itself a unique ID, and hands you ready-to-use URLs. When you're done, tear it down. Zero cleanup.

Whether you're working from your laptop, a tablet, or someone else's machine — your full dev environment is one URL away.

## Features

- **Self-registering containers** — auto-assigns ID, configures routing, builds `APP_URL_*` env vars
- **Flexible routing** — choose between Traefik (self-managed) or Cloudflare Tunnels (zero open ports)
- **VSCode Web** — browser-based IDE on port 8000
- **noVNC** — visual desktop access on port 8002
- **Chromium CDP** — headless browser automation on port 9222
- **5 app slots** — routed via Traefik with configurable tags (e.g. `api`, `app`, `dashboard`)
- **Project setup scripts** — `.openclaw/setup.sh` convention for automated repo setup
- **nvm** — Node version management, reads `.nvmrc` automatically

## Architecture

```
+-----------------------------------------------------------+
|  Devbox Container (ghcr.io/adshrc/openclaw-devbox:latest) |
|                                                           |
|  +----------+  +----------+  +-------------------+        |
|  |  VSCode  |  |  noVNC   |  |  Chromium (CDP)   |        |
|  |  :8000   |  |  :8002   |  |  :9222            |        |
|  +----------+  +----------+  +-------------------+        |
|                                                           |
|       App 1 :8003   App 2 :8004   App 3 :8005             |
|       App 4 :8006   App 5 :8007                           |
+----------------------------+------------------------------+
                             |
                  +-----------------------+
                  |  Traefik/Cloudflared  |
                  +-----------------------+
                             |
             Browser: https://{tag}-{id}.{domain}
```

## Prerequisites

### 1. Docker Socket Access

The OpenClaw container needs access to the Docker daemon on the host to manage devbox containers. Start your OpenClaw container with these additional flags:

```bash
-v /var/run/docker.sock:/var/run/docker.sock
-v /usr/bin/docker:/usr/bin/docker:ro
```

On the host, set the correct permissions to make the Docker socket accessible:

```bash
chmod 666 /var/run/docker.sock
```

> **Note:** This must be done on the host machine before starting the OpenClaw container. If the container is already running, restart it after adding the volume mounts (e.g. docker-compose.yml)

> **Important:** The host path mapped to `/home/node/.openclaw` inside the OpenClaw container must **not** be a system directory (e.g. `/etc`, `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/run`, `/var/run`). Use a dedicated path like `/home/openclaw` or `/opt/openclaw` instead.

### 2. Routing: Traefik or Cloudflare Tunnels

Devboxes need a way to expose services via URLs. Choose one:

#### Option A: Traefik (self-managed reverse proxy)

Best for: servers with an existing Traefik setup.

> **If you haven't set up Traefik yet**, follow the [OpenClaw + Traefik Setup Guide](https://gist.github.com/adshrc/3cd9e8a714098f414635b7fe1ab5e573#file-openclaw_traefik-md).

Devbox containers automatically register Traefik routes on startup by writing config files to the Traefik config directory.

Your OpenClaw container needs the Traefik config directory mounted. Start your OpenClaw container with this additional flag (if not within /home/openclaw):

```bash
-v path_to_traefik:/home/node/.openclaw/traefik
```

Make sure that you have a wildcard DNS record (`*.example.com`) pointing to your server.

#### Option B: Cloudflare Tunnels (zero open ports)

Best for: environments without a reverse proxy, behind NAT, or where you don't want to expose any ports.

Each devbox starts `cloudflared` internally and registers DNS records via the Cloudflare API. All traffic is routed through Cloudflare's network — no open ports or Traefik needed.

Requirements:

- A **Cloudflare account** with a domain managed by Cloudflare
- A **Cloudflare API token** with Zone:DNS:Edit and Account:Tunnel:Edit permissions

During onboarding, the agent will:

1. Validate your Cloudflare API token
2. Look up the Zone ID for your domain
3. Create a named tunnel (`openclaw-devboxes`)
4. Store `CF_API_AUTH`, `CF_ZONE_ID`, `CF_ACCOUNT_ID`, `CF_TUNNEL_ID`, and `CF_TUNNEL_AUTH` in the agent config

## How to install and use

### 1. Install the skill

```bash
npx clawhub@latest install devboxes
```

**OR**

```bash
git clone https://github.com/adshrc/openclaw-devboxes-skill
```

then copy the `SKILL.md` and `references/` directories into your OpenClaw workspace:

```
/home/node/.openclaw/workspace/skills/devboxes/
├── SKILL.md
└── references/
    └── setup-script-guide.md
```

### 2. Ask your agent to set it up

Once installed, simply ask your OpenClaw agent:

> "Set up the devboxes skill"

The agent will read the skill's onboarding instructions and handle everything:

- Pull the Docker image
- Set up the counter file and permissions
- Configure `openclaw.json` with the devboxes agent
- Ask you for your domain and GitHub token

### 3. Spawn a devbox

After setup, just ask:

> "Spin up a devbox for project X"

The agent spawns a container, waits for self-registration, and returns your URLs.

## Bind Mounts

The devbox agent config maps these paths from the OpenClaw container into each devbox:

| Agent path                             | Devbox container path     | Purpose                                   |
| -------------------------------------- | ------------------------- | ----------------------------------------- |
| `/home/node/.openclaw/.devbox-counter` | `/shared/.devbox-counter` | ID counter                                |
| `/home/node/.openclaw/traefik/configs` | `/traefik`                | Traefik route configs (Traefik mode only) |

> **Important:** Both paths must be writable by sandbox containers (UID 1000). The counter file needs `chmod 666`, and the Traefik devboxes dir should be owned by `1000:1000`.

## Self-Registration

Each container's entrypoint automatically:

1. Reads and increments the shared counter → assigns `DEVBOX_ID`
2. Builds `APP_URL_1..5`, `VSCODE_URL`, `NOVNC_URL` from tags + domain + ID
3. Writes env vars to `/etc/devbox.env` and `/etc/profile.d/devbox.sh` (available in all shells)
4. Routes based on `ROUTING_MODE`:
   - **Traefik** (default): Writes config to `/traefik/devbox-{id}.yml`
   - **Cloudflare Tunnel**: Generates cloudflared ingress config, registers DNS CNAME records via CF API, starts `cloudflared tunnel run`

No manual routing or ID assignment needed.

The devbox working directory is `/workspace`. Cloned repos should live under `/workspace/<repo>`.

## Cleanup

OpenClaw manages the container lifecycle — containers are removed when sessions end. Traefik route configs left behind are harmless.

## Project Setup Scripts

Projects can include `.openclaw/setup.sh` for automated setup inside a devbox:

```bash
#!/bin/bash
export NVM_DIR="/root/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

nvm install && nvm use
npm install

cp template.env .env
sed -i "s/PORT=.*/PORT=$APP_PORT_1/" .env

tmux new -d -s my-server "source /root/.nvm/nvm.sh; nvm use; npm run dev; exec \$SHELL"
echo "Running at $APP_URL_1"
```

See [setup-script-guide.md](references/setup-script-guide.md) for full conventions.

## Environment Variables

### Static (set in openclaw.json sandbox.docker.env)

| Variable          | Example                    | Description                                          |
| ----------------- | -------------------------- | ---------------------------------------------------- |
| `ROUTING_MODE`    | `traefik` or `cloudflared` | Routing backend (default: `traefik`)                 |
| `GH_AUTH`         | `ghp_...`                  | GitHub PAT for cloning                               |
| `DEVBOX_DOMAIN`   | `example.com`              | Base domain                                          |
| `APP_TAG_1..5`    | `app1`, `app2`, ...        | Route tags (e.g. use "app1" as "api")                |
| `ENABLE_VNC`      | `true`                     | Enable noVNC                                         |
| `ENABLE_VSCODE`   | `true`                     | Enable VSCode Web                                    |
| `CF_TUNNEL_AUTH`  | `eyJ...`                   | Cloudflare tunnel run token (cloudflared only)       |
| `CF_API_AUTH`     | `abc123`                   | CF API token for DNS registration (cloudflared only) |
| `CF_ZONE_ID`      | `xyz789`                   | CF zone ID for the domain (cloudflared only)         |
| `CF_TUNNEL_ID`    | `uuid`                     | CF tunnel ID for CNAME targets (cloudflared only)    |

### Dynamic (built by entrypoint, available in all shells)

| Variable        | Example                                | Description                 |
| --------------- | -------------------------------------- | --------------------------- |
| `DEVBOX_ID`     | `1`                                    | Auto-assigned sequential ID |
| `APP_URL_1..5`  | `https://app1-1.example.com`           | Full URLs per app slot      |
| `APP_PORT_1..5` | `8003..8007`                           | Internal ports              |
| `VSCODE_URL`    | `https://vscode-1.example.com`         | VSCode Web URL              |
| `NOVNC_URL`     | `https://novnc-1.example.com/vnc.html` | noVNC URL                   |

## Ports

| Port      | Service                        |
| --------- | ------------------------------ |
| 8000      | VSCode Web                     |
| 8002      | noVNC                          |
| 9222      | Chrome DevTools Protocol (CDP) |
| 8003-8007 | App slots 1-5                  |

## Browser

The devbox agent has browser access via Chromium CDP on port 9222. The subagent can use the `browser` tool to navigate, screenshot, and interact with apps running inside the container (use `http://localhost:{port}`).

## Important Notes

- Sandbox containers run with **all Linux capabilities dropped** (`CapDrop: ALL`). Bind-mounted files/dirs must be world-writable.
- The devbox working directory is always `/workspace`.

## License

MIT
