import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_cli_command():
    return [sys.executable, "-m", "agentic_video_editor"]


@pytest.fixture
def run_ave():
    """Run the public AVE CLI.

    Set AVE_CLI_COMMAND to adapt these tests while the package entry point is
    still settling, for example: AVE_CLI_COMMAND="python -m ave.cli".
    """

    raw_command = os.environ.get("AVE_CLI_COMMAND")
    base_command = shlex.split(raw_command) if raw_command else _default_cli_command()

    def _run(*args, check=True, timeout=30):
        env = os.environ.copy()
        python_paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        result = subprocess.run(
            [*base_command, *map(str, args)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            pytest.fail(
                "AVE CLI command failed\n"
                f"command: {shlex.join([*base_command, *map(str, args)])}\n"
                f"exit code: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    return _run


def parse_json_stdout(result):
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            "Expected JSON on stdout\n"
            f"error: {exc}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


@pytest.fixture
def list_assets_json(run_ave):
    """Return assets from `ave assets PROJECT --json`.

    The command shape is intentionally centralized here so implementation churn
    only needs one test helper update if the CLI settles on a different flag.
    """

    def _list(project_dir):
        payload = parse_json_stdout(run_ave("assets", project_dir, "--json"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("assets"), list):
            return payload["assets"]
        pytest.fail(
            "Expected `ave assets PROJECT --json` to return a JSON list or an "
            f"object with an assets list; got: {payload!r}"
        )

    return _list
