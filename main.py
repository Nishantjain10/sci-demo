"""
Scilab Web Executor API — production FastAPI backend for executing Scilab code.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

EXECUTION_TIMEOUT_SECONDS = 10
MAX_CODE_LENGTH = 100_000
TMP_DIR = Path("/tmp")
SCILAB_HOME = TMP_DIR / "scilab-home"

# Default: scilab-cli (-nb -f). Set SCILAB_BINARY=scilab-adv-cli for headless plot export with Xvfb.
SCILAB_BINARY = os.environ.get("SCILAB_BINARY", "scilab-cli")

app = FastAPI(
    title="Scilab Web Executor API",
    description="Execute Scilab code strings and return stdout, stderr, and plot images.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExecuteRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=MAX_CODE_LENGTH)


class ExecuteResponse(BaseModel):
    success: bool
    output: str
    error: str
    plot_base64: Optional[str] = None


def _build_wrapper_script(user_code: str, execution_id: str) -> str:
    """Wrap user code with plot-export hooks and a known output path."""
    plot_path = (TMP_DIR / f"{execution_id}_plot.png").as_posix()
    return f"""// Scilab Web Executor — auto-generated wrapper ({execution_id})
__PLOT_FILE__ = "{plot_path}";

// --- User code begin ---
{user_code}
// --- User code end ---

// Export the most recently opened figure, if any.
try
    fig_ids = winsid();
    if ~isempty(fig_ids) then
        xs2png(max(fig_ids), __PLOT_FILE__);
    end
catch
    // No figure to export, or graphics unavailable in this mode.
end
"""


def _resolve_scilab_binary() -> str:
    """Resolve the Scilab CLI binary, preferring the configured default."""
    if shutil.which(SCILAB_BINARY):
        return SCILAB_BINARY
    if SCILAB_BINARY != "scilab-cli" and shutil.which("scilab-cli"):
        logger.warning(
            "%s not found; falling back to scilab-cli (plots may be unavailable).",
            SCILAB_BINARY,
        )
        return "scilab-cli"
    raise RuntimeError(
        f"Scilab binary '{SCILAB_BINARY}' not found. Install scilab-cli in the container."
    )


def _build_command(sce_path: Path) -> list[str]:
    """Build the xvfb-wrapped Scilab batch command."""
    scilab_bin = _resolve_scilab_binary()
    return [
        "xvfb-run",
        "-a",
        "--server-args=-screen 0 1024x768x24",
        scilab_bin,
        "-nb",
        "-f",
        str(sce_path),
        "-quit",
    ]


def _ensure_scilab_home() -> None:
    SCILAB_HOME.mkdir(parents=True, exist_ok=True)


def _cleanup_execution_artifacts(
    execution_id: str,
    sce_path: Path,
    plot_path: Path,
) -> None:
    """Remove temporary script, plot, and any Scilab temp artifacts for this run."""
    paths_to_remove = [
        sce_path,
        plot_path,
        TMP_DIR / f"{execution_id}.sci",
        TMP_DIR / f"{execution_id}.sce~",
    ]

    for path in paths_to_remove:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", path, exc)

    for pattern in (f"{execution_id}*", f"scilab_{execution_id}*"):
        for artifact in TMP_DIR.glob(pattern):
            try:
                if artifact.is_file():
                    artifact.unlink()
            except OSError as exc:
                logger.warning("Failed to delete %s: %s", artifact, exc)


def _read_plot_base64(plot_path: Path) -> Optional[str]:
    if not plot_path.is_file():
        return None
    try:
        if plot_path.stat().st_size == 0:
            return None
        return base64.b64encode(plot_path.read_bytes()).decode("ascii")
    except OSError as exc:
        logger.warning("Failed to read plot file %s: %s", plot_path, exc)
        return None


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate the Scilab process group safely after a timeout."""
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _run_scilab(sce_path: Path) -> subprocess.CompletedProcess[str]:
    """Execute Scilab under Xvfb with a strict timeout."""
    env = os.environ.copy()
    env["HOME"] = str(SCILAB_HOME)

    cmd = _build_command(sce_path)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        preexec_fn=os.setsid,
    )

    try:
        stdout, stderr = process.communicate(timeout=EXECUTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise exc

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
async def execute_scilab(request: ExecuteRequest) -> ExecuteResponse | JSONResponse:
    execution_id = str(uuid.uuid4())
    sce_path = TMP_DIR / f"{execution_id}.sce"
    plot_path = TMP_DIR / f"{execution_id}_plot.png"

    output = ""
    error = ""
    success = False
    plot_base64: Optional[str] = None

    try:
        _ensure_scilab_home()
        wrapped_code = _build_wrapper_script(request.code, execution_id)
        sce_path.write_text(wrapped_code, encoding="utf-8")

        try:
            completed = _run_scilab(sce_path)
        except subprocess.TimeoutExpired:
            return JSONResponse(
                status_code=408,
                content={
                    "success": False,
                    "output": "",
                    "error": (
                        f"Scilab execution timed out after {EXECUTION_TIMEOUT_SECONDS} seconds."
                    ),
                    "plot_base64": None,
                },
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "plot_base64": None,
                },
            )

        output = completed.stdout
        error = completed.stderr
        success = completed.returncode == 0
        plot_base64 = _read_plot_base64(plot_path)

        return ExecuteResponse(
            success=success,
            output=output,
            error=error,
            plot_base64=plot_base64,
        )
    finally:
        _cleanup_execution_artifacts(execution_id, sce_path, plot_path)
