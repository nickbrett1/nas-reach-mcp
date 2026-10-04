# Fixtures: synthetic on purpose

These are **not** a recording of the real host. They are hand-built to the exact
shape of the Docker API, because this repository is public and a real
`/containers/json` dump is a map of the homelab: container names, bridge IPs,
published ports and compose project names. Publishing that to test a resolver
would be a bad trade.

What they *do* reproduce faithfully, because each one is a measured property of
the real API:

| Property | Where it is encoded |
|---|---|
| `GET /networks` returns `"Containers": {}` for every network | `networks_summary.json` (feeds the trap test) |
| Membership exists only on `GET /networks/{name}` | `networks/*.json` |
| `Aliases` is `null`, not a list | every entry in `networks/*.json` |
| Host-network containers have `"Networks": {}` **and** `"Ports": []` | `metrics` |
| `HostConfig.NetworkMode` is the network name, not `"bridge"` | every container |
| A published binding's `PrivatePort` is the container-side port | `shelf`: `3005 -> 3000` |
| A bridge is a container on two networks | `registry`, `shelf` |
| A container on no network is unreachable by name | `metrics` |
| A container with no published port has no visible port | `db` |

The `shelf` container is the case worth reading first: `3005 -> 3000` means it is
`http://shelf:3000` from `svc_net` and `http://nas:3005` from the host, which is
the entire reason this tool takes a `from` argument.

## Capturing real fixtures instead

For a private fork, record the live API from a container with `docker.sock`:

```sh
curl -s --unix-socket /var/run/docker.sock http://localhost/v1.43/containers/json > containers.json
for n in $(curl -s --unix-socket /var/run/docker.sock http://localhost/v1.43/networks | python3 -c 'import sys,json;print(" ".join(n["Name"] for n in json.load(sys.stdin)))'); do
  curl -s --unix-socket /var/run/docker.sock "http://localhost/v1.43/networks/$n" > "networks/$n.json"
done
```

Then scrub names, IPs and labels before committing anywhere public.
