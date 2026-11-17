# Python Serverless

A text-analysis API demonstrating container-based serverless with FastAPI and
Knative. Run locally with Docker Compose, or use Knative to explore request-driven
autoscaling and scale to zero.

Use Docker Engine with Compose, or [uv](https://docs.astral.sh/uv/getting-started/installation/)
0.13.5 to run the Python application locally. Python 3.14.9 is pinned in the project;
uv installs it if needed. Run the commands below in Bash from the repository root.

## Docker Compose

```bash
docker compose up --build -d --wait --wait-timeout 30
curl --fail-with-body http://127.0.0.1:8000/health
docker compose logs --tail 30 app
```

The API is available at `http://127.0.0.1:8000`. To use another host port, run
`export APP_HOST_PORT=8001` before starting. Stop with `docker compose down`.

## Local Python

```bash
uv sync --project app --locked
uv run --project app --locked python -m serverless_lab --host 127.0.0.1 --port 8000
```

Stop with `Ctrl+C`. This uses the same port as Compose: run one option at a time,
or choose a different port with `--port`.

## API

- `GET /health` — health check.
- `POST /analyze` — text analysis.
- `POST /review-text` — text analysis and word-count validation.
- `/docs` — interactive API documentation.

In another terminal, while the application is running:

```bash
curl --fail-with-body http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello, world! Hello.","top_k":5}'
curl --fail-with-body http://127.0.0.1:8000/review-text \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello, world! Hello.","min_words":4,"max_words":8}'
```

## Checks

Run application and operational-script tests, Ruff, mypy, and the Python package build:

```bash
bash scripts/check.sh
```

Check the Docker image in a disposable Compose project:

```bash
docker compose build
python3 -m scripts.checks.compose_smoke --image python-serverless-lab:0.1.0
```

## Local Knative

Requires Linux amd64, Docker, uv, Python 3.12+, at least 6 CPUs, 6 GiB of available
RAM, and 30 GiB of free disk space. Tools are installed locally under `.local/`.

```bash
docker compose build
python3 -m scripts.cluster setup
python3 -m scripts.cluster deploy
uv run --project app --locked python -m scripts.checks.scaling --concurrency 20 --duration 45
python3 -m scripts.cluster cleanup
```

Ingress: `http://127.0.0.1:8081`, with `Host: text-analysis.serverless-lab.lab.test`.
Experiment reports are saved under `.local/runs/`. Run `cleanup` even if the
experiment fails; it removes only the cluster owned by this project.

## CI

GitHub Actions runs the same checks, builds the Docker image, and runs the Compose
smoke check on pushes and pull requests to `main`. Knative is tested separately locally.
