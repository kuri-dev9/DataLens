# DataLens Phase 0 test-server deployment

## Topology and ownership

This repository deploys only the versioned `datalens:0.1.0` image. QueryForge and
Ollama remain independently deployed services, and MySQL is reachable only through
QueryForge. External testers call the published DataLens HTTP port; they do not need
QueryForge endpoints or credentials.

QueryForge must be deployed using its own image and Compose contract. Its named volume
mounted at `/var/lib/queryforge` retains Parquet datasets, `datasets.sqlite3`, the
catalog snapshot, and interaction logs across container replacement. DataLens does not
create or manage that volume. QueryForge must be configured with a read-only MySQL
account and its catalog refresh must be completed before the live Agent test.

## Prerequisites

- Docker Engine with the Docker Compose plugin
- `curl` on the test server
- A running QueryForge deployment and a DataLens-specific QueryForge API key
- A running Ollama service with the configured model already installed
- Network reachability from the DataLens container to both services
- A read-only MySQL target reachable from QueryForge

Do not use `127.0.0.1` for a dependency running outside the DataLens container. Use a
container DNS name on a shared operator-managed network, or a routable server hostname/IP.
On Docker Desktop only, `host.docker.internal` can address a host service. The supplied
Compose file intentionally does not own or join QueryForge/Ollama lifecycle.

## Configure

```sh
cp .env.example .env
vi .env
```

Replace every placeholder and set at least:

- `DATALENS_API_KEY`
- `DATALENS_QUERYFORGE_API_KEY`
- `DATALENS_QUERYFORGE_ENDPOINT`, ending in `/mcp`
- `DATALENS_QUERYFORGE_DATA_BASE_URL`, the same QueryForge origin without `/mcp`
- `DATALENS_OLLAMA_BASE_URL` and `DATALENS_OLLAMA_MODEL`
- request, session, Agent, QueryForge, and Ollama timeout/policy values as needed

`DATALENS_HTTP_HOST=0.0.0.0` is required inside the container. `DATALENS_BIND_HOST` and
`DATALENS_BIND_PORT` control host publication. Keep the default bind host `127.0.0.1`
unless a reverse proxy, firewall, and test-server access policy explicitly permit remote
access. Completed `.env` files must remain server-side and uncommitted. Docker/Compose
secret injection may be used instead, provided the same environment names reach the
container.

## CPU-only embedding server (optional)

DataLens learns from successful turns and needs an embedding model to recall them.
By default it reuses the LLM Ollama, which loads `bge-m3` onto the GPU. When GPU
memory is tight, Ollama evicts the LLM to make room and reloads it on the next call;
DataLens embeds twice per turn, so a tight GPU turns into two model reloads per turn.

`docker-compose.embeddings.yml` runs a second Ollama with no GPU devices attached.
This is physical isolation rather than a `CUDA_VISIBLE_DEVICES` convention, so the
embedding model cannot reach the GPU regardless of the host configuration.

```sh
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml up -d
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml \
  exec ollama-embed ollama pull bge-m3
```

Then point DataLens at it. Both services share the Compose project network, so the
service name resolves directly.

```sh
DATALENS_EMBEDDING_BASE_URL=http://ollama-embed:11434
```

`bge-m3` is a 568M-parameter model and each turn embeds two short strings, so CPU
latency stays far below a single LLM round trip. An OpenAI-compatible server such as
`llama-server --embeddings` works too; set `DATALENS_EMBEDDING_API=openai`.

If the embedding server is unreachable, turns still complete without recall. Check
`GET /v1/ready` — `checks.memory` reports `ok`, `unavailable`, or `disabled`.

## Deploy and check

```sh
./dl.sh preflight
./dl.sh rebuild
./dl.sh status
./dl.sh health
./dl.sh smoke
```

`preflight` validates configuration syntax, placeholders, Docker, and Compose without
contacting dependencies. `health` checks unauthenticated liveness and authenticated
QueryForge/Ollama readiness. `smoke` creates and deletes a DataLens session without an
LLM completion.

After QueryForge, its external MySQL, and Ollama are ready, run the final live path:

```sh
./dl.sh agent-check
```

This performs three Korean turns through DataLens HTTP only and deletes the session in a
`finally` path. It does not call QueryForge HTTP/MCP directly.

Operational commands:

```sh
./dl.sh logs
./dl.sh restart
./dl.sh down
./dl.sh up
```

`down` does not delete volumes. DataLens has no persistent application volume in Phase 0;
its SessionStore is in memory. Container logs go to stdout/stderr and are available through
`docker compose logs`.

## Shutdown behavior

Compose sends SIGTERM and allows 40 seconds before forced termination. The ASGI lifespan
stops accepting new work, releases every remaining QueryForge Application Session on a
best-effort basis, then closes Ollama HTTP and MCP resources. A failed release neither
blocks other sessions nor exposes the QueryForge session identifier. Complex active-turn
draining remains a live-verification item.

## Versioning and health

The default image is `datalens:0.1.0`, independently versioned from QueryForge. Override
`DATALENS_IMAGE` only with another immutable/versioned release tag; do not rely on `latest`
as the sole deployment identity. The Docker healthcheck uses `/v1/health` and needs no
secret. `/v1/ready` requires the DataLens API key and is intentionally checked by
`./dl.sh health`, not by Docker.
