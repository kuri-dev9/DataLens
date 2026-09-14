# DataLens

DataLens is the HTTP application and bounded orchestration layer above the independently deployed QueryForge MCP server.

For a test-server Docker deployment, follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Foundation development

Python 3.11 or later is required.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[test]"
cp .env.example .env
```

Export the configured values from `.env`, then run:

```sh
.venv/bin/datalens
```

Phase 0 endpoints:

- `GET /v1/health`
- `GET /v1/ready`
- `POST /v1/sessions`
- `POST /v1/sessions/{session_id}/messages`
- `DELETE /v1/sessions/{session_id}`

`/v1/health` is unauthenticated liveness. Every other endpoint, including
`/v1/ready`, requires `x-api-key: <DATALENS_API_KEY>`.

The container operational helper uses only the DataLens HTTP API for checks:

```sh
./dl.sh up
./dl.sh health
./dl.sh smoke
# Run only during the separate live verification with all dependencies ready:
./dl.sh agent-check
```

See `./dl.sh` usage for preflight, restart, rebuild, status, logs, and down. Container
logs are written to stdout/stderr and viewed with `docker compose logs`.

The message endpoint is wired to the bounded Agent, Ollama provider, and QueryForge MCP client. It returns an explicit allowlisted upstream error when either configured service is unavailable; it never returns fake analytical results.

Run deterministic non-LLM tests with:

```sh
.venv/bin/python -m pytest -q -m "not integration"
```
