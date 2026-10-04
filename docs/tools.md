# The tool contract

One question: **what is the address of service X, from vantage point Y, and is it up?**

A port is absolute; an address is relative. `litellm:4000` and `nas:4000` name
the same gateway, and neither is *the* address.

## Vantage

`from_vantage` — named `from_vantage` because `from` is a Python keyword, but it
is the design's `from`. It is **required** on `resolve_service` and
`check_reachable` and is **never defaulted**: a default is how you get a
confidently wrong address.

| Value | Means |
|---|---|
| `host` | the NAS itself, or any `network_mode: host` context |
| `network:<name>` | a container attached to that network |
| `container:<name>` | *through* a specific container |
| `self` | this process's own vantage, detected |

A bare word (`ai_proxy`) is read as a network name.

## Tools

| Tool | Question |
|---|---|
| `whoami` | where am I standing? |
| `list_networks` | what networks exist, who is on them, and which containers bridge them? |
| `list_services(from_vantage)` | what is reachable from here, and at what address? |
| `resolve_service(name, from_vantage)` | where does this name answer from there — or why not? |
| `check_reachable(name_or_url, from_vantage)` | does it actually answer, right now? |
| `describe_service(name)` | what is this thing attached to? |

## Status vocabulary

`resolve_service` always answers specifically. Returning empty is a failure mode.

| Status | Meaning |
|---|---|
| `resolved` | a candidate address exists for that vantage |
| `host_only` | runs `network_mode: host`; no name on any network, reachable by host port or not at all |
| `not_attached` | exists and has networks, but not this one — the *name* will not resolve there |
| `unresolvable_name` | no such container; DSM packages and host processes are invisible to the Docker API |
| `unknown_port` | attached and nameable, but no port is visible to the Docker API |
| `ambiguous` | two containers share the name; guessing would be confidently wrong |
| `unknown_network` / `unknown_container` | the vantage itself does not exist |

`not_attached` means the **name** does not resolve. It does not mean the address
is unreachable: Docker bridges route to each other through the host, so a
container's bridge IP often still answers. That is host routing rather than
Docker DNS, so it is reported in a note and never offered as *the* answer.

## Probe outcomes

`check_reachable` never returns an opaque `000`. `curl` reports one status code
for three different diseases; they are separated here:

| Outcome | Evidence |
|---|---|
| `unresolvable_name` | the name never resolved (`time_namelookup` is zero) |
| `refused` | resolved, but the port actively refused the connection |
| `timeout` | resolved, connection attempt timed out — filtered |
| `unreachable` | resolved, but there is no route |
| `ok` | connected and spoke HTTP |

`ok` carries the HTTP status, the `Server` header, latency, and — for an MCP path
— `serverInfo` from a single `initialize` handshake. Identity is part of
liveness: an HTTP 200 from the wrong service is worse than a 404.

## Honest limits

- **The probe runs in this process's network namespace.** Deployed
  `network_mode: host`, that means it confirms host-vantage answers directly, and
  for a network vantage it probes the bridge IP (which is what actually answers)
  while reporting the name-based address the caller would use. When it cannot
  confirm the requested vantage, it says so rather than implying success.
- **Nothing is cached.** Every call re-reads runtime state, because runtime state
  goes stale: one network gained a member inside a single day while this tool was
  being designed.
- **`verified_at` is on every response**, including graph-derived ones.
- **It is not a CMDB, a config reconciler, or a secret resolver.** It reports
  runtime; if a compose file disagrees, that is not this tool's problem.
