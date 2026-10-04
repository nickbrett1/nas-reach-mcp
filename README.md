# nas-reach-mcp

> **What is the address of service X, from vantage point Y — and is it up?**

A read-only [MCP](https://modelcontextprotocol.io) server that answers
reachability questions about a Docker host. It reads the live Docker runtime
(containers, networks, attachments and published ports) and reports the
correct address *for the vantage point you name* — or, just as importantly,
explains precisely why there isn't one.

It is built for a homelab where the same service is reachable at several
different addresses depending on where you are standing, and where guessing
wrongly is the normal outcome.

## Why this exists: a port is absolute, an address is relative

`litellm:4000` and `nas:4000` can name the **same** gateway, and neither is
*the* address. Docker's embedded DNS resolves container names on a
user-defined network, but the host's own hostname does not resolve *inside*
that network — so:

- from the **host**, the address is `nas:4000`;
- from a container on the **`ai_proxy` network**, it is `litellm:4000`.

Reachability is therefore a graph query, not a lookup: a container is
reachable from a network if and only if it is attached to that network, and
multi-homed containers act as **bridges** between networks. Getting this wrong
returns a plausible-looking URL that simply doesn't work — which is exactly
the failure this tool exists to prevent.

## The six tools

| Tool | Question it answers |
|---|---|
| `whoami` | Where am I standing? (this process's own vantage, detected) |
| `list_networks` | What networks exist, who is on them, and which containers bridge them? |
| `list_services(from_vantage)` | What is reachable from here, and at what address? |
| `resolve_service(name, from_vantage)` | Where does this name answer from there — or why not? |
| `check_reachable(name_or_url, from_vantage)` | Does it actually answer, right now? |
| `describe_service(name)` | What is this thing attached to? (networks, IPs, published ports) |

## The vantage model

`from_vantage` is required on `resolve_service` and `check_reachable` and is
**never defaulted** — a default is how you get a confidently wrong address.
(`list_services` is the one exception: "everything from where I am" is a
complete question on its own.)

| Value | Means |
|---|---|
| `host` | the Docker host itself, or any `network_mode: host` context |
| `network:<name>` | a container attached to that network |
| `container:<name>` | *through* a specific container (i.e. its networks) |
| `self` | this process's own vantage, detected |

A bare word (`ai_proxy`) is read as a network name.

## Answers are specific, not empty

`resolve_service` always answers with a reason. Returning nothing is a failure
mode, so a negative result names what it checked and points at where the
service *is* reachable:

- `resolved` — a candidate address exists for that vantage
- `host_only` — runs `network_mode: host`; no name on any network, so it is
  reachable by host port or not at all
- `not_attached` — exists and has networks, but not this one (the *name* will
  not resolve there; a bridge IP may still answer via host routing)
- `unresolvable_name` — no such container (DSM packages and host processes are
  invisible to the Docker API)
- `unknown_port` — attached and nameable, but no port is visible to the API
- `ambiguous` — two containers share the name; guessing would be wrong
- `unknown_network` / `unknown_container` — the vantage itself doesn't exist

`check_reachable` never collapses failures into an opaque `000`. `curl` reports
one status code for several different diseases, so they are separated:
`unresolvable_name` (the name never resolved), `refused` (port actively
refused), `timeout` (filtered), `unreachable` (no route), or `ok`. A successful
probe also reports the HTTP status, `Server` header and latency, and — on an MCP
path — `serverInfo` from a single `initialize` handshake, because an HTTP 200
from the *wrong* service is worse than a 404.

Nothing is cached: every call re-reads runtime state, and every response is
stamped `verified_at`.

The full tool contract lives in [`docs/tools.md`](docs/tools.md).

## Deployment

The server is designed to be deployed to the Docker host itself, because it
needs the host's view of the runtime:

- **Network mode: `host`** — so the `host` vantage is genuinely the host and
  host-network containers are visible. The trade-off: this process has no name
  on any Docker network, so it can only resolve host names and ports.
- **`/var/run/docker.sock` mounted read-only** (note: a read-only mount does not
  make the socket read-only — it is root-equivalent either way).
- **Port `3010`** — [`mcpo`](https://github.com/open-webui/mcpo) wraps the stdio
  server as Streamable HTTP, served at the root (`/openapi.json`, `/docs`).
- Registered in **MCPHub** as slug `nas-reach` (type `openapi`, group `ops`) and
  surfaced on the **Homepage** dashboard.

See [`deploy/README.md`](deploy/README.md) for the full runbook
(Buildkite → GHCR → Watchtower → Docker host, plus rollback).

```bash
docker compose up -d
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NAS_REACH_HOSTNAME` | `nas` | The host name to use when composing host-vantage addresses |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Docker API socket path |
| `NAS_REACH_SELF_CONTAINER` | *(unset)* | Override self-container detection when hostname matching fails |

> The `MCPHUB_*` variables in the generated `.env.example` / compose file are
> genproj scaffolding and are **not read by this code**.

### Known limitation

Because it runs `network_mode: host`, `check_reachable` probes from the
**host** vantage. It confirms a host-vantage answer directly; for a network
vantage it probes the container's bridge IP (which is what actually answers)
while reporting the name-based address the caller would use. The
`confirms_requested_vantage` flag is derived from that evidence, not asserted:
`confirmed_by` says which probe established it (`by-name` or `by-ip`), and when
a probe answers but is not evidence for the requested vantage, `confirms_note`
says why instead of leaving a bare `false`. Truly probing from inside a
container vantage requires running inside that vantage (e.g. an ephemeral
container on the target network), which this read-only tool does not do.

## Relationship to `nas-port-mcp`

This is a sibling to `nas-port-mcp`. `nas-port-mcp` answers *which host port
maps to which container*; `nas-reach-mcp` answers the harder, vantage-relative
question — *what address works from where, and does it answer*. The two
complement each other.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Run the checks (the exact sequence CI runs):

```bash
ruff check src tests
pytest -q
```

CI runs on **Buildkite** (see [`.buildkite/`](.buildkite/)); the pipeline's
`docker_publish` step builds the `linux/amd64` image and pushes it to
`ghcr.io/nickbrett1/nas-reach-mcp`.

## Doppler

This project uses Doppler for secrets from the shared `common` project
(config `dev`) — no per-repo Doppler project is created. First use:

```bash
doppler setup --project common --config dev
```

The Doppler CLI is installed in the devcontainer; auth is persisted via the
host `~/.doppler` bind-mount.

### Env-var precedence (read this if `doppler run` hits the wrong project)

Doppler resolves its target as **environment variables > `doppler.yaml` >
`~/.doppler` scoped config**. If your shell — or the session that launched the
devcontainer (e.g. an agent runtime) — exports `DOPPLER_PROJECT` /
`DOPPLER_CONFIG` / `DOPPLER_ENVIRONMENT`, those silently override this repo's
`doppler.yaml`. To force the correct context manually:

```bash
unset DOPPLER_PROJECT DOPPLER_CONFIG DOPPLER_ENVIRONMENT
doppler setup --no-interactive --project common --config dev
```

## The container's agent

This devcontainer brings up its own `a2a-goose` agent, registered in the hub as
`nas-reach-mcp-dev` — one agent per repo, so a restart reclaims the same entry
instead of adding a second one. Turns are billed through the LiteLLM proxy
configured in Doppler (`LITELLM_BASE_URL`).

```bash
scripts/agent-dev.sh start    # write secrets + config, fetch the launcher, run it
scripts/agent-dev.sh status   # running or not, the card URL, the log tail
scripts/agent-dev.sh stop     # SIGTERM, wait for a clean deregister, confirm gone
```

`start` runs from the devcontainer's post-start hook, so the agent is normally
already up when you arrive. It fails open: with no network on a first start it
prints why it did not start and leaves the project usable. Secrets come from
Doppler into `~/.config/a2a-goose/env` (mode 0600) and never into the image or
`containerEnv`.

## Generated by genproj

This project was scaffolded with [genproj](https://github.com/nickbrett1/genproj);
the application code above is hand-written.
