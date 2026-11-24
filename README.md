# SNCF MCP

[![sncf-mcp MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Keized/sncf-mcp/badges/card.svg)](https://glama.ai/mcp/servers/Keized/sncf-mcp)

Serveur MCP pour les horaires, retards en temps réel et perturbations des trains SNCF (TER, TGV, Intercités), utilisable depuis Claude, ChatGPT, Cursor et tout client MCP.

MCP server for SNCF train schedules, delays and disruptions, built on the [SNCF API](https://api.sncf.com). Read-only, compact answers, times in Europe/Paris. Remote MCP over streamable HTTP at `/mcp`.

## Tools

| Tool | Purpose |
|---|---|
| `find_station(query)` | Station ids by name |
| `departures(station_id, from_datetime?, count?)` | Next departures with realtime delay, cancellation and skipped stop |
| `journeys(from_station, to_station, from_datetime?, count?)` | Journeys with legs and realtime delays |
| `disruptions(station_id? \| line_id?)` | Current and upcoming disruptions (service alerts) |

## Realtime

Delays, cancellations, skipped stops and disruptions come from the SNCF GTFS-RT feeds (trip updates and service alerts) of the dataset [Réseau SNCF TGV, Intercités et TER](https://transport.data.gouv.fr), published on transport.data.gouv.fr under the Licence Ouverte. Feeds need no token and are cached for 30 s. A train absent from the feed runs as planned; if the feed is down, tools return planned times with `realtime_unavailable`.

## Configuration

| Variable | Default | |
|---|---|---|
| `SNCF_API_TOKEN` | | Required, SNCF API key |
| `SNCF_API_URL` | `https://api.sncf.com/v1/coverage/sncf` | SNCF API coverage |
| `MCP_AUTH_TOKEN` | | Optional. If set, requests need `Authorization: Bearer <token>` |
| `PORT` | `8830` | Listen port |
| `HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the image) |

### API token

1. Request a free token on the [SNCF developer page](https://www.digital.sncf.com/startup/api/token-developpeur). It arrives by email.
2. Copy `.env.example` to `.env` at the repo root and set `SNCF_API_TOKEN=<your token>`.

`.env` is ignored by git and never baked into the image: Docker Compose reads it from the repo root at startup, and the local run loads it into the shell. On a server, the `.env` sits next to `compose.yaml`.

## Run locally

```sh
uv sync
set -a; . ./.env; set +a
uv run sncf-mcp
```

Serves `http://127.0.0.1:8830/mcp`. For clients that launch the server themselves over stdio:

```sh
uv run sncf-mcp --transport stdio
```

## Run with Docker

```sh
docker compose up -d --build
```

Serves `http://127.0.0.1:8830/mcp`, bound to localhost only.

Or pull the published image:

```sh
docker run -d --env-file .env -p 127.0.0.1:8830:8830 ghcr.io/keized/sncf-mcp:latest
```

## Connect

### Claude Code (same machine)

```sh
claude mcp add --transport http sncf http://127.0.0.1:8830/mcp
```

With `MCP_AUTH_TOKEN` set, add `--header "Authorization: Bearer <token>"`.

### claude.ai and ChatGPT (public HTTPS on a VPS)

Both clients call the server from the internet, so it needs a public HTTPS URL.

1. **DNS**: create an `A` record (and `AAAA` for IPv6) such as `sncf.example.com` pointing to the VPS IP. Do not skip it: without it the certificate cannot be issued.
2. **Reverse proxy with TLS**, for example Caddy on the VPS, which gets the certificate itself:

   ```caddyfile
   sncf.example.com {
       reverse_proxy 127.0.0.1:8830
   }
   ```

   If Caddy runs in Docker, put it on the same network as this container and proxy to `sncf-mcp:8830` instead.
3. **Protect it**: set `MCP_AUTH_TOKEN` (for example `openssl rand -hex 32`). Without it anyone who finds the URL uses your SNCF API quota.
4. **Check**: `curl -i https://sncf.example.com/mcp` must answer 401.
5. **Add the connector** with the URL `https://sncf.example.com/mcp`:
   - claude.ai: Settings, Connectors, Add custom connector, then in Request headers (beta) add `Authorization` with the value `Bearer <token>`.
   - ChatGPT: Plugins, +, Add custom MCP server. It only offers OAuth or no authentication, so it cannot send the token. To use it anyway, leave `MCP_AUTH_TOKEN` empty and hide the server behind a secret path instead: `handle_path /<openssl rand -hex 24>/* { reverse_proxy 127.0.0.1:8830 }` plus `respond 404`, and use `https://sncf.example.com/<secret>/mcp` as the URL.

### Other clients

Use `http://127.0.0.1:8830/mcp` on the same machine, the public URL otherwise. Drop the header when `MCP_AUTH_TOKEN` is empty.

- **Cursor**, `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

  ```json
  { "mcpServers": { "sncf": {
      "url": "https://sncf.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:SNCF_MCP_TOKEN}" } } } }
  ```

- **Devin** (runs in the cloud, public URL only): Settings, MCP servers, add a custom MCP server, transport HTTP, the URL, and the header `Authorization: Bearer <token>`.
- **OpenClaw**:

  ```sh
  openclaw mcp add sncf --url https://sncf.example.com/mcp --transport streamable-http --header "Authorization=Bearer <token>"
  ```

  Set `--transport` explicitly: OpenClaw defaults to SSE.
- **Hermes Agent**, `~/.hermes/config.yaml`, then `/reload-mcp`:

  ```yaml
  mcp_servers:
    sncf:
      url: https://sncf.example.com/mcp
      headers:
        Authorization: "Bearer ${SNCF_MCP_TOKEN}"
  ```

  `SNCF_MCP_TOKEN` must be set in the Hermes profile environment, otherwise the request fails with 401.

## Development

Tests, lint and types run in Docker without installing anything:

```sh
d() { docker compose run --rm --user "$(id -u):$(id -g)" dev "$@"; }
d uv run ruff check . && d uv run ruff format --check . && d uv run mypy && d uv run pytest
```

Release: set the same version in `pyproject.toml`, `uv.lock` and `server.json`, merge, then push a tag `vX.Y.Z`. The `publish` workflow pushes the image to GHCR and the server to the MCP registry.

## License

[Apache 2.0](LICENSE): free to use, modify and redistribute, provided you keep the copyright and the [NOTICE](NOTICE) crediting this project.
