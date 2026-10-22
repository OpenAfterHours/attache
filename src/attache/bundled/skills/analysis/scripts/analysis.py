# /// script
# requires-python = ">=3.12"
# dependencies = ["hailer==0.2.8"]
# ///
"""Use Hailer's isolated notebooks from Attaché, without a second model or agent loop.

    analysis.py <workspace> <command> [value]

Commands: init, build, import, start, stop, status, list, create, open, cells, execute.

Workspaces live in ./analysis/<name>, under the current chat's files folder. Only copies of
explicitly imported datasets and the notebooks are mounted. Kernels survive script calls
and Attaché restarts: Hailer records ownership in .hailer/kernel.json, outside both mounts.
`stop` removes only Docker resources labelled with this workspace. Notebook Python never
runs on the host; Hailer model/provider configuration is never loaded.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

VERSION = "0.2.8"
MARIMO_VERSION = "0.24.2"
DATA_SUFFIXES = {".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson", ".xlsx", ".xls"}
CONFIG = """[hailer]
notebook = "notebooks/analysis.py"
notebooks_dir = "notebooks"
data_dir = "data"

[kernel]
runtime = "docker"
network = false
memory = "4g"
cpus = 2
"""


def dependency() -> SimpleNamespace:
    """Load only Hailer's notebook/runtime interfaces, pinned to the tested contract."""
    from importlib.metadata import version

    import hailer
    from hailer import (
        browser,
        kernel,
        kernel_docker,
        kernel_image,
        marimo_client,
        models,
        notebooks,
    )
    from hailer.tools import HailerTools

    if hailer.__version__ != VERSION or version("marimo") != MARIMO_VERSION:
        raise ValueError(f"Use hailer=={VERSION} with marimo=={MARIMO_VERSION}; rerun via uv run.")
    if "server" not in inspect.signature(HailerTools).parameters:
        raise ValueError("Use the PyPI Hailer 0.2.8 release; this checkout has a different API.")
    logger = logging.getLogger("hailer")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return SimpleNamespace(
        browser=browser,
        client=marimo_client,
        kernel=kernel,
        docker=kernel_docker,
        image=kernel_image,
        models=models,
        notebooks=notebooks,
        tools=HailerTools,
    )


def inside(root: Path, path: Path) -> Path:
    """Resolve a path and refuse escapes through links or traversal."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("The path must stay inside this chat's analysis workspace.")
    return resolved


def workspace(name: str) -> Path:
    """Find this chat's named workspace without accepting a user-supplied directory."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) or re.fullmatch(
        r"con|prn|aux|nul|com[0-9]|lpt[0-9]", name
    ):
        raise ValueError("Use a workspace name such as sales or monthly-budget.")
    root = inside(Path.cwd(), Path.cwd() / "analysis" / name)
    for part in (
        "data",
        "notebooks",
        "notebooks/analysis.py",
        ".hailer",
        ".hailer/kernel.json",
        ".hailer/notebook.json",
        ".config",
        "hailer.toml",
    ):
        inside(root, root / part)
    return root


def configuration(root: Path, api: Any) -> Any:
    """Build a fixed Docker configuration; host environment overrides are never consulted."""
    return api.models.HailerConfig(
        workspace=root,
        notebook=root / "notebooks" / "analysis.py",
        notebooks_dir=root / "notebooks",
        data_dir=root / "data",
        context_dir=root / ".config" / "context",
        skills_dir=root / ".config" / "skills",
        prompts_dir=root / ".config" / "prompts",
        config_path=root / "hailer.toml",
        kernel=api.models.KernelConfig(runtime="docker", network=False),
    )


def initialise(config: Any, api: Any) -> str:
    """Create a persistent empty notebook, preserving an existing workspace."""
    for folder in (config.data_dir, config.notebooks_root):
        folder.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        config.config_path.write_text(CONFIG, encoding="utf-8")
    if not config.notebook.exists():
        notebook = api.notebooks.create_notebook(config, "analysis", kind="empty")
        api.notebooks.save_active_notebook(config, notebook)
    return f"Ready: {config.workspace}. Notebook Python runs only in Docker; run start next."


def import_data(config: Any, value: str) -> str:
    """Copy a selected dataset from this chat; never overwrite existing data."""
    source = inside(Path.cwd(), Path.cwd() / value)
    if not source.is_file() or source.suffix.lower() not in DATA_SUFFIXES:
        raise ValueError("Choose a CSV, TSV, Parquet, JSON, JSONL or Excel file in this chat.")
    target = inside(config.workspace, config.data_dir / source.name)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    return f"Copied {source.name}; notebook path: /work/data/{source.name} (read-only)."


def safe_state(config: Any, api: Any, *, stopping: bool = False) -> Any:
    """Refuse an inherited local or differently configured runtime before connecting."""
    state = api.kernel.read_kernel_state(config.workspace)
    if state and state.runtime != "docker":
        raise ValueError(
            "This workspace has a local runtime; stop it in Hailer before using analysis."
        )
    if state and not stopping:
        expected = {
            str(config.notebooks_root.resolve()): "/work/notebooks",
            str(config.data_dir.resolve()): "/work/data",
        }
        mounted = {str(Path(host).resolve()): target for host, target in state.mounts}
        if (
            state.hailer_version != VERSION
            or mounted != expected
            or api.docker.settings_mismatch(state, config)
        ):
            raise ValueError("The saved kernel has different settings; run stop, then start.")
    return state


def free_port() -> int:
    """Choose a loopback port; a concurrent bind fails safely in Hailer startup."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def dispatch(args: argparse.Namespace, config: Any, api: Any) -> str:
    """Execute one owner-requested operation using the versioned Hailer adapter."""
    command, value = args.command, args.value
    if command == "init":
        return initialise(config, api)
    if command != "stop" and not config.config_path.is_file():
        raise ValueError("This workspace is not initialised; run init first.")
    if command == "build":
        code = api.image.build(
            config.kernel.effective_image,
            api.docker.SubprocessDockerRunner(),
            no_host_config=True,
        )
        if code:
            raise RuntimeError(
                "Kernel image build failed; check Docker's output, then retry build."
            )
        return "Built the compatible Docker kernel image. Run start to open the notebook."
    if command == "import":
        return import_data(config, value)
    if command == "list":
        active = api.notebooks.load_active_notebook(config)
        rows = [
            f"{n.path.name}{' (active)' if n.path == active else ''}"
            for n in api.notebooks.list_notebooks(config)
        ]
        return "\n".join(rows) or "No saved notebooks."
    state = safe_state(config, api, stopping=command == "stop")
    runtime = api.docker.DockerRuntime(config)
    if command == "stop":
        removed = runtime.remove_leftovers(running=True)
        if removed.failed:
            raise RuntimeError(
                "Could not stop all workspace resources; retry stop with Docker running."
            )
        if state:
            api.kernel.delete_kernel_state(config.workspace, token=state.token)
        return "Stopped this workspace's Docker resources. Saved notebooks and datasets are kept."
    running = runtime.find_running()
    if command == "start" and running is None:
        running = runtime.start(free_port())
    if running is None:
        return "ERROR: Notebook runtime is not reachable; run start with Docker running."
    server = running.server
    if server.runtime != "docker" or server.network_access or not server.token:
        raise ValueError(
            "Refusing a runtime without authenticated, network-isolated Docker execution."
        )
    opened: list[bool] = []

    def show(url: str) -> bool:
        success = api.browser.open_url(url)
        opened.append(success)
        return success

    bridge = api.tools(config, server=server, open_url=show)
    if command in {"start", "open", "create"}:
        notebook = value or str(api.notebooks.load_active_notebook(config))
        result = (
            bridge.notebook_create(value, template="empty")
            if command == "create"
            else bridge.notebook_open(notebook)
        )
        if result.startswith("ERROR:"):
            return result
        active = api.notebooks.load_active_notebook(config)
        # Hailer reuses live sessions even after the browser tab closes. Explicit open
        # must still show the notebook, without opening a second tab for a new session.
        if not opened:
            url = api.client.open_notebook_url(server, active, view="edit", with_token=True)
            show(url)
        if not opened[-1]:
            return "ERROR: Notebook saved/selected, but the browser could not open; retry open."
        # PyPI 0.2.8 describes bring-up failures without an ERROR prefix. Check the
        # actual session instead of treating its human-readable outcome as success.
        client = api.client.MarimoClient(
            server.url,
            token=server.token,
            notebook=active,
            workspace=config.workspace,
            paths=server.paths,
        )
        client.resolve_session(active)
        return result
    if command == "status":
        return bridge.marimo_status()
    if command == "cells":
        return bridge.notebook_cells(value)
    return bridge.marimo_execute(value)


def redact(text: str, root: Path) -> str:
    """Keep the notebook bearer token out of model-visible results, including errors."""
    try:
        token = json.loads((root / ".hailer" / "kernel.json").read_text(encoding="utf-8")).get(
            "token"
        )
        if isinstance(token, str) and token:
            text = text.replace(token, "<redacted>")
    except (OSError, ValueError, AttributeError):
        pass
    text = re.sub(r"(?i)(access_token=)[^\s&\"'<>]+", r"\1<redacted>", text)
    return re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1<redacted>", text)


def main(argv: list[str] | None = None) -> int:
    """Run a command; dependency imports and all filesystem writes follow the role check."""
    if os.environ.get("ATTACHE_ROLE", "owner") != "owner":
        print("Analysis is available only in the owner's own chat.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "build",
            "import",
            "start",
            "stop",
            "status",
            "list",
            "create",
            "open",
            "cells",
            "execute",
        ),
    )
    parser.add_argument("value", nargs="?", default="")
    args = parser.parse_args(argv)
    if args.command in {"import", "create", "execute"} and not args.value.strip():
        parser.error(f"{args.command} needs a value")
    root = None
    try:
        root = workspace(args.workspace)
        api = dependency()
        result = dispatch(args, configuration(root, api), api)
        print(redact(result, root))
        return int(result.startswith(("ERROR:", "Execution failed.")))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(redact(message, root) if root else message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
