# Scilab Web Executor API

Production-ready FastAPI backend that executes Scilab code submitted by a web frontend and returns stdout, stderr, and optional plot images as base64.

## Quick start (3 steps)

### 1. Build the Docker image

From this directory:

```bash
docker build -t scilab-web-executor .
```

### 2. Run the container locally

```bash
docker run --rm -p 8000:8000 scilab-web-executor
```

The API listens on [http://localhost:8000](http://localhost:8000).

### 3. Execute Scilab code

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "disp(2 + 2); x = 0:0.1:2*%pi; plot2d(x, sin(x));"}'
```

Example response:

```json
{
  "success": true,
  "output": " 4.\n\n",
  "error": "",
  "plot_base64": "<base64-encoded PNG or null>"
}
```

## Deploy to Render or Railway

Both platforms run this service from the included `Dockerfile`.

1. Push this folder to a Git repository (GitHub, GitLab, etc.).
2. Create a new **Web Service** and connect the repo.
3. Configure:
   - **Runtime:** Docker
   - **Port:** `8000`
   - **Health check path:** `/health`
   - **Environment variable (recommended):** `SCILAB_BINARY=scilab-adv-cli` for plot export

**Render:** New → Web Service → Environment: Docker → Root Directory: `scilab-web-executor` (if nested) → Deploy.

**Railway:** New Project → Deploy from GitHub → Railway auto-detects the `Dockerfile` → set public port to `8000`.

The container image sets `SCILAB_BINARY=scilab-adv-cli` by default so `plot2d` and similar commands export PNG output under Xvfb. Use `SCILAB_BINARY=scilab-cli` only for numeric/script execution without plots.

## API

| Method | Path        | Description                          |
|--------|-------------|--------------------------------------|
| GET    | `/health`   | Liveness check                       |
| POST   | `/execute`  | Run Scilab code; 10s timeout max     |

**POST `/execute` body:**

```json
{ "code": "your scilab code here" }
```

**Responses:**

- `200` — execution finished (check `success` for exit status)
- `408` — execution exceeded the 10-second timeout
- `500` — server or Scilab binary misconfiguration

CORS is enabled for all origins (`*`) so browser frontends can call the API directly.
