"""
Scilab Web Executor API.

Fast text execution with scilab-cli and optional
background plot generation with scilab-adv-cli.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

FAST_TIMEOUT_SECONDS = 10
PLOT_TIMEOUT_SECONDS = 60

MAX_CODE_LENGTH = 100_000

TMP_DIR = Path("/tmp")
SCILAB_HOME = TMP_DIR / "scilab-home"


app = FastAPI(
    title="Scilab Web Executor API",
    description="Execute Scilab code and generate plots.",
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
    code: str = Field(
        ...,
        min_length=1,
        max_length=MAX_CODE_LENGTH,
    )


class ExecuteResponse(BaseModel):
    success: bool
    output: str
    error: str
    plot_job_id: Optional[str] = None


class PlotJobResponse(BaseModel):
    status: str
    plot_base64: Optional[str] = None
    error: Optional[str] = None


# In-memory plot jobs.
# This is intentionally simple for the presentation/demo.
plot_jobs: dict[str, dict] = {}


GRAPHICS_FUNCTIONS = [
    "plot",
    "plot2d",
    "plot2d1",
    "plot2d2",
    "plot2d3",
    "plot2d4",
    "plot2d5",
    "plot3d",
    "plot3d1",
    "surf",
    "mesh",
    "histplot",
    "champ",
    "contour",
    "grayplot",
    "Matplot",
    "param3d",
    "param3d1",
    "polarplot",
    "xtitle",
    "xlabel",
    "ylabel",
    "zlabel",
    "xgrid",
    "xset",
    "xsetech",
    "xs2png",
    "xs2jpg",
    "xs2svg",
    "xs2eps",
]


@app.get("/")
async def root():
    return FileResponse("/app/index.html")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _contains_graphics(code: str) -> bool:
    """Detect whether the submitted code contains graphics commands."""

    for function_name in GRAPHICS_FUNCTIONS:
        pattern = rf"\b{re.escape(function_name)}\s*\("
        if re.search(pattern, code):
            return True

    return False


def _remove_graphics_lines(code: str) -> str:
    """
    Remove common single-line graphics commands from the fast
    no-graphics execution.

    The full original code is still used by the background
    graphics execution.
    """

    graphics_pattern = "|".join(
        re.escape(name)
        for name in GRAPHICS_FUNCTIONS
    )

    pattern = re.compile(
        rf"^\s*(?:{graphics_pattern})\s*\(.*\)\s*;?\s*(?://.*)?$",
        re.MULTILINE,
    )

    return pattern.sub(
        "// Graphics command handled in background.\n",
        code,
    )


def _write_script(
    code: str,
    execution_id: str,
) -> Path:
    """Write Scilab code to a temporary script."""

    path = TMP_DIR / f"{execution_id}.sce"

    path.write_text(
        code,
        encoding="utf-8",
    )

    return path


def _run_process(
    command: list[str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess safely with a timeout."""

    logger.info(
        "Running command: %s",
        " ".join(command),
    )

    env = os.environ.copy()
    env["HOME"] = str(SCILAB_HOME)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        preexec_fn=os.setsid,
    )

    try:
        stdout, stderr = process.communicate(
            timeout=timeout,
        )

    except subprocess.TimeoutExpired:
        logger.error(
            "Process timed out after %s seconds.",
            timeout,
        )

        _terminate_process(process)

        raise

    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def _terminate_process(
    process: subprocess.Popen[str],
) -> None:
    """Terminate the entire process group."""

    if process.poll() is not None:
        return

    try:
        os.killpg(
            os.getpgid(process.pid),
            signal.SIGTERM,
        )

        process.wait(timeout=2)

    except (
        ProcessLookupError,
        subprocess.TimeoutExpired,
    ):
        pass

    if process.poll() is None:
        try:
            os.killpg(
                os.getpgid(process.pid),
                signal.SIGKILL,
            )
        except ProcessLookupError:
            pass

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _find_binary(name: str) -> str:
    """Find a Scilab executable."""

    path = shutil.which(name)

    if not path:
        raise RuntimeError(
            f"Scilab binary '{name}' was not found."
        )

    return path


def _run_fast_execution(
    code: str,
    execution_id: str,
) -> tuple[str, str, bool]:
    """
    Execute code using scilab-cli.

    Graphics commands are removed because scilab-cli does not
    provide graphics. The original code is separately executed
    by the background plot worker.
    """

    fast_code = _remove_graphics_lines(code)

    script_path = _write_script(
        fast_code,
        f"{execution_id}_fast",
    )

    try:
        scilab_cli = _find_binary(
            "scilab-cli",
        )

        command = [
            scilab_cli,
            "-nb",
            "-nouserstartup",
            "-noatomsautoload",
            "-f",
            str(script_path),
            "-quit",
        ]

        try:
            completed = _run_process(
                command,
                FAST_TIMEOUT_SECONDS,
            )

        except subprocess.TimeoutExpired:
            return (
                "",
                "Scilab calculation timed out.",
                False,
            )

        return (
            completed.stdout,
            completed.stderr,
            completed.returncode == 0,
        )

    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def _build_plot_wrapper(
    user_code: str,
    execution_id: str,
) -> tuple[str, Path]:
    """Build the graphics-enabled Scilab script."""

    plot_path = (
        TMP_DIR / f"{execution_id}_plot.png"
    )

   wrapped_code = f"""
// Scilab Cloud background graphics execution

__PLOT_FILE__ = "{plot_path.as_posix()}";

// --- User code ---
{user_code}
// --- End user code ---

// Export the latest figure.
try
    fig_ids = winsid();

    if ~isempty(fig_ids) then
        xs2png(max(fig_ids), __PLOT_FILE__);
    end
catch
    // No plot was generated.
end
"""

    script_path = _write_script(
        wrapped_code,
        f"{execution_id}_plot",
    )

    return script_path, plot_path


def _generate_plot(
    job_id: str,
    user_code: str,
) -> None:
    """
    Generate the plot after the main HTTP response has
    already been returned.
    """

    plot_script = None
    plot_path = None

    try:
        plot_jobs[job_id]["status"] = "running"

        _ensure_scilab_home()

        plot_script, plot_path = _build_plot_wrapper(
            user_code,
            job_id,
        )

        scilab_adv_cli = _find_binary(
            "scilab-adv-cli",
        )

        command = [
            "xvfb-run",
            "-a",
            "--server-args=-screen 0 1024x768x24",
            scilab_adv_cli,
            "-nb",
            "-nouserstartup",
            "-noatomsautoload",
            "-f",
            str(plot_script),
            "-quit",
        ]

        try:
            completed = _run_process(
                command,
                PLOT_TIMEOUT_SECONDS,
            )

        except subprocess.TimeoutExpired:
            plot_jobs[job_id] = {
                "status": "failed",
                "plot_base64": None,
                "error": (
                    "Plot generation timed out after "
                    f"{PLOT_TIMEOUT_SECONDS} seconds."
                ),
            }
            return

        if completed.returncode != 0:
    plot_jobs[job_id] = {
        "status": "failed",
        "plot_base64": None,
        "error": completed.stderr or (
            "Scilab plot generation failed."
        ),
    }
    return
            plot_jobs[job_id] = {
                "status": "failed",
                "plot_base64": None,
                "error": completed.stderr or (
                    "Scilab plot generation failed."
                ),
            }
            return

        if not plot_path.is_file():
            plot_jobs[job_id] = {
                "status": "failed",
                "plot_base64": None,
                "error": "No plot image was generated.",
            }
            return

        image_data = base64.b64encode(
            plot_path.read_bytes()
        ).decode("ascii")

        plot_jobs[job_id] = {
            "status": "complete",
            "plot_base64": image_data,
            "error": None,
        }

        logger.info(
            "Plot job %s completed.",
            job_id,
        )

    except Exception as exc:
        logger.exception(
            "Plot job %s failed.",
            job_id,
        )

        plot_jobs[job_id] = {
            "status": "failed",
            "plot_base64": None,
            "error": str(exc),
        }

    finally:
        if plot_script:
            try:
                plot_script.unlink()
            except OSError:
                pass

        if plot_path:
            try:
                plot_path.unlink()
            except OSError:
                pass


def _ensure_scilab_home() -> None:
    SCILAB_HOME.mkdir(
        parents=True,
        exist_ok=True,
    )


@app.post(
    "/execute",
    response_model=ExecuteResponse,
)
async def execute_scilab(
    request: ExecuteRequest,
    background_tasks: BackgroundTasks,
) -> ExecuteResponse:

    _ensure_scilab_home()

    execution_id = str(uuid.uuid4())

    has_graphics = _contains_graphics(
        request.code,
    )

    output, error, success = _run_fast_execution(
        request.code,
        execution_id,
    )

    plot_job_id = None

    if has_graphics:
        plot_job_id = str(uuid.uuid4())

        plot_jobs[plot_job_id] = {
            "status": "queued",
            "plot_base64": None,
            "error": None,
        }

        background_tasks.add_task(
            _generate_plot,
            plot_job_id,
            request.code,
        )

    # Graphics errors from the fast CLI are intentionally
    # removed because graphics are handled separately.
    return ExecuteResponse(
        success=success,
        output=output,
        error=error if not has_graphics else "",
        plot_job_id=plot_job_id,
    )


@app.get(
    "/plot/{job_id}",
    response_model=PlotJobResponse,
)
async def get_plot(
    job_id: str,
) -> PlotJobResponse:

    job = plot_jobs.get(job_id)

    if not job:
        return PlotJobResponse(
            status="not_found",
            plot_base64=None,
            error="Plot job not found.",
        )

    return PlotJobResponse(
        status=job["status"],
        plot_base64=job["plot_base64"],
        error=job["error"],
    )
