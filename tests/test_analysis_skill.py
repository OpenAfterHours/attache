"""The optional notebook bridge: no installed Hailer, Docker, browser or model needed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills


@pytest.fixture
def analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "attache_analysis", skills.BUNDLED / "analysis" / "scripts" / "analysis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATTACHE_ROLE", raising=False)
    return module


@pytest.fixture
def api(analysis: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = SimpleNamespace(
        state=None,
        running=None,
        calls=[],
        cleanup_failed=[],
        bring_up=False,
        session_ready=True,
    )

    def config(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs, notebooks_root=kwargs["notebooks_dir"])

    def create(cfg: Any, name: str, *, kind: str) -> Path:
        fake.calls.append(("create", name, kind))
        path = cfg.notebooks_root / f"{name}.py"
        path.write_text("import marimo\napp = marimo.App()\n", encoding="utf-8")
        return path

    def active(cfg: Any) -> Path:
        path = cfg.workspace / ".hailer" / "notebook.json"
        return Path(json.loads(path.read_text(encoding="utf-8"))["active"])

    def save(cfg: Any, path: Path) -> None:
        folder = cfg.workspace / ".hailer"
        folder.mkdir(exist_ok=True)
        (folder / "notebook.json").write_text(json.dumps({"active": str(path)}), encoding="utf-8")

    def record(cfg: Any) -> Any:
        return SimpleNamespace(
            runtime="docker",
            hailer_version="0.2.8",
            token="secret-runtime-token",
            mounts=(
                (str(cfg.notebooks_root), "/work/notebooks"),
                (str(cfg.data_dir), "/work/data"),
            ),
            network_access=False,
        )

    class Runtime:
        def __init__(self, cfg: Any) -> None:
            self.cfg = cfg

        def find_running(self) -> Any:
            fake.calls.append(("find", self.cfg.workspace))
            return fake.running

        def start(self, port: int) -> Any:
            fake.calls.append(("start", port))
            fake.state = record(self.cfg)
            path = self.cfg.workspace / ".hailer" / "kernel.json"
            path.write_text(json.dumps({"token": fake.state.token}), encoding="utf-8")
            fake.running = SimpleNamespace(
                server=SimpleNamespace(
                    runtime="docker",
                    network_access=False,
                    token=fake.state.token,
                    url="http://127.0.0.1:27180",
                    paths=None,
                )
            )
            return fake.running

        def remove_leftovers(self, *, running: bool) -> Any:
            fake.calls.append(("remove-labelled", self.cfg.workspace, running))
            if not fake.cleanup_failed:
                fake.running = None
            return SimpleNamespace(failed=fake.cleanup_failed)

    def remove_record(root: Path, *, token: str) -> None:
        assert token == fake.state.token
        fake.calls.append(("delete-state", root))
        fake.state = None
        (root / ".hailer" / "kernel.json").unlink(missing_ok=True)

    class Bridge:
        def __init__(self, cfg: Any, *, server: Any, open_url: Any) -> None:
            self.cfg = cfg
            self.open_url = open_url
            assert server is fake.running.server

        def notebook_open(self, notebook: str) -> str:
            fake.calls.append(("open", notebook))
            if fake.bring_up:
                launched = self.open_url("http://127.0.0.1/?access_token=secret-runtime-token")
                if not launched:
                    return "analysis.py is now active. Could not open a browser."
                if not fake.session_ready:
                    return "analysis.py is now active. No kernel session appeared within 30 s."
            return "kernel session ready"

        def notebook_create(self, name: str, *, template: str) -> str:
            path = create(self.cfg, name, kind=template)
            save(self.cfg, path)
            return "Created notebook. " + self.notebook_open(str(path))

        def notebook_cells(self, pattern: str) -> str:
            fake.calls.append(("cells", pattern))
            return "saved cell, errors=0"

        def marimo_status(self) -> str:
            return "docker ready; data read-only"

        def marimo_execute(self, code: str) -> str:
            fake.calls.append(("execute", code))
            if code == "fail":
                return "Execution failed. ValueError"
            return "rows: 3; total: 42"

    fake.models = SimpleNamespace(
        HailerConfig=config,
        KernelConfig=lambda **kwargs: SimpleNamespace(
            **kwargs, effective_image="ghcr.io/openafterhours/hailer-kernel:0.2.8"
        ),
    )
    fake.browser = SimpleNamespace(open_url=lambda url: fake.calls.append(("browser", url)) or True)

    class Client:
        def __init__(self, url: str, **kwargs: Any) -> None:
            assert url == "http://127.0.0.1:27180"
            assert kwargs["token"] == "secret-runtime-token"

        def resolve_session(self, notebook: Path) -> Any:
            if not fake.session_ready:
                raise RuntimeError(
                    "No open session matches analysis.py; open the notebook browser."
                )
            return SimpleNamespace(session_id="ready")

    fake.client = SimpleNamespace(
        MarimoClient=Client,
        open_notebook_url=lambda server, notebook, **_: (
            f"http://127.0.0.1:27180/?access_token={server.token}&file={notebook}"
        ),
    )
    fake.notebooks = SimpleNamespace(
        create_notebook=create,
        save_active_notebook=save,
        load_active_notebook=active,
        list_notebooks=lambda cfg: [
            SimpleNamespace(path=p) for p in cfg.notebooks_root.glob("*.py")
        ],
    )
    fake.kernel = SimpleNamespace(
        read_kernel_state=lambda _: fake.state,
        delete_kernel_state=remove_record,
    )
    fake.docker = SimpleNamespace(
        DockerRuntime=Runtime,
        SubprocessDockerRunner=lambda: "docker-runner",
        settings_mismatch=lambda state, _: ["network"] if state.network_access else [],
    )
    fake.image = SimpleNamespace(build=lambda *args, **kwargs: 0)
    fake.tools = Bridge
    monkeypatch.setattr(analysis, "dependency", lambda: fake)
    monkeypatch.setattr(analysis, "free_port", lambda: 27180)
    return fake


def test_dataset_notebook_resume_and_stop(
    analysis: ModuleType, api: Any, tmp_path: Path, capsys: Any
) -> None:
    assert analysis.main(["sales", "init"]) == 0
    root = tmp_path / "analysis" / "sales"
    (tmp_path / "sales.csv").write_text("value\n12\n14\n16\n", encoding="utf-8")
    assert analysis.main(["sales", "import", "sales.csv"]) == 0
    assert (root / "data" / "sales.csv").read_bytes() == (tmp_path / "sales.csv").read_bytes()
    assert analysis.main(["sales", "start"]) == 0
    assert analysis.main(["sales", "execute", "print(total)"]) == 0
    assert analysis.main(["sales", "cells"]) == 0
    assert analysis.main(["sales", "create", "quarterly"]) == 0
    assert analysis.main(["sales", "init"]) == 0  # does not reset active notebook or data
    assert analysis.main(["sales", "start"]) == 0  # another process turn reuses durable state
    assert len([c for c in api.calls if c[0] == "start"]) == 1
    assert api.calls[-2] == ("open", str(root / "notebooks" / "quarterly.py"))
    assert api.calls[-1][0] == "browser"  # reopening a live session still opens its browser
    assert analysis.main(["sales", "stop"]) == 0
    assert ("remove-labelled", root, True) in api.calls
    assert (root / "notebooks" / "quarterly.py").exists()
    assert (root / "data" / "sales.csv").exists()
    assert analysis.main(["sales", "list"]) == 0
    output = capsys.readouterr().out
    assert "quarterly.py (active)" in output and "total: 42" in output
    assert "secret-runtime-token" not in output


@pytest.mark.parametrize("exit_code", [0, 1])
def test_local_build_uses_pinned_image_without_host_settings(
    analysis: ModuleType, api: Any, exit_code: int
) -> None:
    analysis.main(["sales", "init"])

    def build(image: str, runner: Any, **options: Any) -> int:
        assert image == "ghcr.io/openafterhours/hailer-kernel:0.2.8"
        assert runner == "docker-runner"
        assert options == {"no_host_config": True}
        return exit_code

    api.image.build = build
    assert analysis.main(["sales", "build"]) == exit_code
    assert not any(c[0] == "start" for c in api.calls)


@pytest.mark.parametrize("role", ["member", "guest", "", "Owner"])
def test_owner_check_precedes_imports_and_writes(
    analysis: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("ATTACHE_ROLE", role)
    monkeypatch.setattr(analysis, "dependency", lambda: pytest.fail("loaded dependency"))
    assert analysis.main(["sales", "init"]) == 1
    assert not (tmp_path / "analysis").exists()


@pytest.mark.parametrize(
    "name", ["../elsewhere", "a/b", "a\\b", "C:\\elsewhere", "con", "nul", "a:b"]
)
def test_invalid_workspace_never_loads_dependency(
    analysis: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(analysis, "dependency", lambda: pytest.fail("loaded dependency"))
    assert analysis.main([name, "init"]) == 1


def test_import_refuses_escape_and_overwrite(
    analysis: ModuleType, api: Any, tmp_path: Path
) -> None:
    assert analysis.main(["sales", "init"]) == 0
    dataset = tmp_path / "sales.csv"
    dataset.write_text("original", encoding="utf-8")
    assert analysis.main(["sales", "import", "sales.csv"]) == 0
    dataset.write_text("replacement", encoding="utf-8")
    assert analysis.main(["sales", "import", "sales.csv"]) == 1
    assert (tmp_path / "analysis" / "sales" / "data" / "sales.csv").read_text(
        encoding="utf-8"
    ) == "original"
    assert analysis.main(["sales", "import", "../private.csv"]) == 1
    assert analysis.main(["sales", "import", "script.py"]) == 1


def test_link_escape(analysis: ModuleType, api: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "analysis" / "sales"
    root.mkdir(parents=True)
    try:
        (root / "data").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this Windows host")
    assert analysis.main(["sales", "init"]) == 1
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("bad", ["local", "network", "version", "mount", "token"])
def test_runtime_boundaries(analysis: ModuleType, api: Any, bad: str) -> None:
    assert analysis.main(["sales", "init"]) == 0
    assert analysis.main(["sales", "start"]) == 0
    if bad == "local":
        api.state.runtime = "local"
    elif bad == "network":
        api.state.network_access = True
    elif bad == "version":
        api.state.hailer_version = "9.0.0"
    elif bad == "mount":
        api.state.mounts += (("/private", "/work/private"),)
    else:
        api.running.server.token = ""
    assert analysis.main(["sales", "execute", "print(1)"]) == 1
    assert not any(c[0] == "execute" for c in api.calls)


def test_failed_stop_preserves_state_and_can_retry(analysis: ModuleType, api: Any) -> None:
    analysis.main(["sales", "init"])
    analysis.main(["sales", "start"])
    api.cleanup_failed = ["Docker busy"]
    assert analysis.main(["sales", "stop"]) == 1
    assert api.state is not None
    assert not any(c[0] == "delete-state" for c in api.calls)
    api.cleanup_failed = []
    assert analysis.main(["sales", "stop"]) == 0
    assert api.state is None


def test_stop_does_not_require_a_surviving_notebook(analysis: ModuleType, api: Any) -> None:
    analysis.main(["sales", "init"])
    analysis.main(["sales", "start"])
    root = analysis.workspace("sales")
    (root / "notebooks" / "analysis.py").unlink()
    (root / "hailer.toml").unlink()
    assert analysis.main(["sales", "stop"]) == 0
    assert api.state is None


def test_open_reports_browser_failure(analysis: ModuleType, api: Any, capsys: Any) -> None:
    analysis.main(["sales", "init"])
    analysis.main(["sales", "start"])
    api.browser.open_url = lambda url: False
    assert analysis.main(["sales", "open"]) == 1
    assert "browser could not open" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["start", "open", "create"])
@pytest.mark.parametrize("problem", ["browser", "session"])
def test_hailer_descriptive_bringup_failures_are_not_success(
    analysis: ModuleType, api: Any, capsys: Any, command: str, problem: str
) -> None:
    analysis.main(["sales", "init"])
    analysis.main(["sales", "start"])
    api.bring_up = True
    if problem == "browser":
        api.browser.open_url = lambda url: False
    else:
        api.session_ready = False
    argv = ["sales", command] + (["quarterly"] if command == "create" else [])
    assert analysis.main(argv) == 1
    captured = capsys.readouterr()
    assert ("browser could not open" if problem == "browser" else "No open session") in (
        captured.out + captured.err
    )


def test_failed_start_does_not_open_a_browser(analysis: ModuleType, api: Any) -> None:
    analysis.main(["sales", "init"])

    def fail(self: Any, port: int) -> None:
        raise RuntimeError("Docker is unavailable")

    api.docker.DockerRuntime.start = fail
    assert analysis.main(["sales", "start"]) == 1
    assert not any(c[0] in {"browser", "open"} for c in api.calls)


def test_unavailable_runtime_and_execution_errors_are_not_success(
    analysis: ModuleType, api: Any
) -> None:
    analysis.main(["sales", "init"])
    assert analysis.main(["sales", "execute", "print(1)"]) == 1
    assert not any(c[0] == "start" for c in api.calls)
    analysis.main(["sales", "start"])
    assert analysis.main(["sales", "execute", "fail"]) == 1


def test_tokens_redacted_from_results_and_errors(
    analysis: ModuleType, api: Any, capsys: Any
) -> None:
    analysis.main(["sales", "init"])
    analysis.main(["sales", "start"])
    api.tools.marimo_execute = lambda self, code: (
        "secret-runtime-token https://localhost/?access_token=another&file=x Bearer third"
    )
    assert analysis.main(["sales", "execute", "anything"]) == 0
    captured = capsys.readouterr()
    assert all(
        secret not in captured.out for secret in ("secret-runtime-token", "another", "third")
    )

    def fail(*args: Any) -> None:
        raise RuntimeError("failed with secret-runtime-token Bearer fourth")

    api.tools.marimo_execute = fail
    assert analysis.main(["sales", "execute", "anything"]) == 1
    captured = capsys.readouterr()
    assert "secret-runtime-token" not in captured.err and "fourth" not in captured.err


def test_configuration_ignores_hailer_environment(
    analysis: ModuleType, api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAILER_KERNEL", "local")
    monkeypatch.setenv("HAILER_MARIMO_URL", "http://example.com")
    monkeypatch.setenv("HAILER_DATA_DIR", "C:/private")
    cfg = analysis.configuration(analysis.workspace("sales"), api)
    assert cfg.kernel.runtime == "docker" and not cfg.kernel.network
    assert cfg.data_dir == cfg.workspace / "data"
    assert not hasattr(cfg, "marimo_url")
