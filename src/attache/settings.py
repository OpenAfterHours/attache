"""Owner-only editing of a safe settings projection, preserving file-managed configuration.

The browser never receives raw source: endpoint options, channel strings, identities and
comments may contain credentials. Only an explicit allowlist is projected. Source changes
are surgical and checked against the parsed expected document before any atomic replacement.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from attache import config

MAX_BYTES = 64 * 1024
APIS = {"anthropic", "openai-chat", "openai-responses", "claude-cli"}
ENDPOINT_FIELDS = {"api", "model", "base_url", "key_env"}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(r"""\s*((?:[^="'\n]|"(?:\\.|[^"\\])*"|'[^']*')+)\s*=\s*""")
SCALAR = re.compile(
    r'''(?s)(?:"""(?:\\.|(?!""").)*"""|'''
    + "'''[\\s\\S]*?'''"
    + r"""|"(?:\\.|[^"\\])*"|'[^']*'|[^#\r\n]+)"""
)


class SettingsError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _flatten(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    result = {}
    for key, value in data.items():
        path = (*prefix, key)
        result.update(_flatten(value, path) if isinstance(value, dict) else {path: value})
    return result


def _url_safe(value: Any) -> bool:
    try:
        if not isinstance(value, str) or any(
            char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
        ):
            return False
        url = urlsplit(value)
        valid = url.scheme in {"http", "https"} and url.hostname
        valid = valid and (url.port is None or 0 < url.port <= 65535)
        return bool(valid) and not any((url.username, url.password, url.query, url.fragment))
    except (ValueError, TypeError, AttributeError):
        return False


def _projection(cfg: config.Config, *, status: bool = False) -> dict[str, Any]:
    result = {
        section: {key: getattr(cfg, key) for key in keys}
        for section, keys in config.SECTIONS.items()
    }
    result["limits"] = dict(cfg.limits)
    for section, values in result.items():
        for key, value in values.items():
            expected = int if section == "limits" or key in {"port", "public_port"} else str
            if type(value) is not expected:
                raise SettingsError("Configuration settings have an incorrect type.")
    result["endpoints"] = {}
    for name, endpoint in cfg.endpoints.items():
        if any(
            value is not None and not isinstance(value, str)
            for value in (endpoint.api, endpoint.model, endpoint.base_url, endpoint.key_env)
        ):
            raise SettingsError("Endpoint fields must be strings.")
        item = {"api": endpoint.api, "model": endpoint.model}
        if endpoint.base_url and _url_safe(endpoint.base_url):
            item["base_url"] = endpoint.base_url
        if endpoint.key_env and ENV_NAME.fullmatch(endpoint.key_env):
            item["key_env"] = endpoint.key_env
        if status:
            item["base_url_hidden"] = bool(endpoint.base_url and "base_url" not in item)
            key = item.get("key_env") or {
                "anthropic": "ANTHROPIC_API_KEY",
            }.get(endpoint.api)
            local = item.get("base_url") and urlsplit(item["base_url"]).hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if local and not endpoint.key_env and endpoint.api.startswith("openai-"):
                key = None
            item["credential_status"] = (
                "configured"
                if key and os.environ.get(key)
                else "missing"
                if key
                else "not-required"
            )
            if endpoint.api.startswith("openai-") and not endpoint.key_env and not local:
                item["credential_status"] = "not-configured"
            if endpoint.key_ref:  # not read here, as reading may ask to unlock the store
                item["credential_status"] = "keyring"
        result["endpoints"][name] = item
    result["channels"] = {
        name: {"enabled": options.get("enabled", False)} for name, options in cfg.channels.items()
    }
    return result


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml(data: dict[str, Any]) -> str:
    sections: dict[tuple[str, ...], list[str]] = {}
    for path, value in _flatten(data).items():
        sections.setdefault(path[:-1], []).append(f"{_literal(path[-1])} = {_literal(value)}")
    return (
        "\n\n".join(
            f"[{'.'.join(map(_literal, path))}]\n" + "\n".join(lines)
            for path, lines in sections.items()
        )
        + "\n"
    )


def _parse(text: str, home: Path) -> config.Config:
    try:
        return config.parse(text, home)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        # Parser/config errors may quote values from the unprojected source.
        raise SettingsError(
            "Configuration is invalid; check the editable values and local file."
        ) from exc


def _validate(cfg: config.Config) -> None:
    if not isinstance(cfg.name, str) or not cfg.name.strip():
        raise SettingsError("The assistant name must contain a non-whitespace character.")
    if cfg.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SettingsError("The web host must be a loopback address.")
    for name in [cfg.own_chats, cfg.group_chats, *(c.endpoint for c in cfg.chats.values())]:
        if name is not None and name not in cfg.endpoints:
            raise SettingsError("Every routing and chat endpoint must name an existing endpoint.")
    for endpoint in cfg.endpoints.values():
        if endpoint.api not in APIS or not isinstance(endpoint.model, str) or not endpoint.model:
            raise SettingsError("Every endpoint needs a supported API and a nonempty model.")
    shared = [cfg.group_chats, *(c.endpoint for c in cfg.chats.values() if c.endpoint)]
    if any(cfg.endpoints[name].api == "claude-cli" for name in shared):
        raise SettingsError("Group chats must use an API endpoint, not Claude Code.")


def _merge(text: str, changes: dict[tuple[str, ...], Any]) -> str:
    expected = tomllib.loads(text)
    for path, value in changes.items():
        table = expected
        for key in path[:-1]:
            table = table.setdefault(key, {})
        table[path[-1]] = value
    pending = dict(changes)
    lines = iter(text.splitlines(keepends=True))
    output, section = [], ()
    newline = "\r\n" if "\r\n" in text else "\n"

    def append_missing() -> None:
        for path in list(pending):
            if path[:-1] == section:
                output.append(
                    f"{newline}{_literal(path[-1])} = {_literal(pending.pop(path))}{newline}"
                )

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            output.append(line)
            continue
        if line.lstrip().startswith("["):
            append_missing()
            header = line.strip().replace("[[", "[", 1)
            if line.lstrip().startswith("[["):
                header = header.replace("]]", "]", 1)
            header_tree = tomllib.loads(header + "\n__settings_marker__ = true")
            section = next(iter(_flatten(header_tree)))[:-1]
            output.append(line)
            continue
        statement = line
        while True:
            try:
                tomllib.loads(statement)
                break
            except tomllib.TOMLDecodeError:
                statement += next(lines)
        match = ASSIGNMENT.match(statement)
        key = next(iter(_flatten(tomllib.loads(match[1] + " = 0"))))
        path = (*section, *key)
        if path in pending:
            value = SCALAR.match(statement, match.end())
            statement = (
                statement[: match.end()] + _literal(pending.pop(path)) + statement[value.end() :]
            )
        output.append(statement)
    append_missing()
    added: dict[tuple[str, ...], list[str]] = {}
    for path, value in pending.items():
        added.setdefault(path[:-1], []).append(f"{_literal(path[-1])} = {_literal(value)}{newline}")
    for path, values in added.items():
        output.append(f"{newline}[{'.'.join(map(_literal, path))}]{newline}" + "".join(values))
    merged = "".join(output)
    try:
        if tomllib.loads(merged) == expected:
            return merged
    except tomllib.TOMLDecodeError:
        pass
    raise SettingsError(
        "This file uses inline or dotted tables; edit the affected setting in attache.toml."
    )


class Settings:
    """One running config and bounded, revision-checked edits to its file on disk."""

    def __init__(self, running: config.Config) -> None:
        self.running = copy.deepcopy(running)
        self.path = running.home / "attache.toml"
        self.backup = running.home / "attache.toml.bak"
        self.lock = running.home / ".settings.lock"

    def _read(self, path: Path) -> bytes:
        if path.is_symlink() or path.resolve().parent != self.path.parent.resolve():
            raise SettingsError(
                "Settings files must be regular files inside the Attaché home.", 403
            )
        if not path.exists():
            return b""
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise SettingsError("Settings files must be regular files of at most 64 KB.", 413)
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SettingsError("Settings files are limited to 64 KB.", 413)
        return data

    def _text(self, data: bytes) -> str:
        try:
            return data.decode("utf-8-sig") if data else config._template("you", "claude")
        except UnicodeDecodeError as exc:
            raise SettingsError("The settings file must use UTF-8.") from exc

    def _snapshot(self, data: bytes, text: str | None = None) -> dict[str, Any]:
        cfg = _parse(self._text(data) if text is None else text, self.path.parent)
        backup = self._read(self.backup)
        return {
            "revision": hashlib.sha256(data).hexdigest(),
            "toml": _toml(_projection(cfg)),
            "saved": _projection(cfg, status=True),
            "running": _projection(self.running, status=True),
            "restart_required": cfg != self.running,
            "backup_available": bool(backup),
            "backup_revision": hashlib.sha256(backup).hexdigest() if backup else None,
        }

    def read(self) -> dict[str, Any]:
        """Return safe editable TOML and saved/running views; never return file source."""
        return self._snapshot(self._read(self.path))

    def _candidate(self, data: bytes, payload: dict[str, Any]) -> str:
        if payload.get("revision") != hashlib.sha256(data).hexdigest():
            raise SettingsError("Settings changed on disk. Reload them before saving.", 409)
        if payload.get("restore") is True:
            backup = self._read(self.backup)
            if not backup:
                raise SettingsError("No previous settings backup is available.", 404)
            if payload.get("backup_revision") != hashlib.sha256(backup).hexdigest():
                raise SettingsError("The backup changed. Reload it before restoring.", 409)
            text = self._text(backup)
            _validate(_parse(text, self.path.parent))
            return text
        text = self._text(data)
        original = _projection(_parse(text, self.path.parent))
        flat = _flatten(original)
        allowed = dict(flat)
        for name in original["endpoints"]:
            for field in ENDPOINT_FIELDS:
                allowed.setdefault(("endpoints", name, field), "")
        if "toml" in payload and "changes" not in payload and isinstance(payload["toml"], str):
            try:
                proposed = _flatten(tomllib.loads(payload["toml"]))
            except tomllib.TOMLDecodeError as exc:
                raise SettingsError("Editable TOML is invalid; check its syntax.") from exc
            if flat.keys() - proposed.keys():
                raise SettingsError(
                    "Keep existing editable fields; settings cannot be removed here."
                )
        elif (
            "changes" in payload and "toml" not in payload and isinstance(payload["changes"], dict)
        ):
            names = {".".join(path): path for path in allowed}
            if len(names) != len(allowed) or payload["changes"].keys() - names.keys():
                raise SettingsError("Only the listed editable settings may be changed.")
            proposed = {**flat, **{names[k]: v for k, v in payload["changes"].items()}}
        else:
            raise SettingsError("Send either editable TOML or a settings changes object.")
        for path, value in proposed.items():
            if path not in allowed or type(value) is not type(allowed[path]):
                raise SettingsError("Editable settings contain an unknown field or incorrect type.")
            if path[0] == "endpoints" and path[-1] == "base_url" and value and not _url_safe(value):
                raise SettingsError(
                    "Endpoint URLs must use HTTP(S) without credentials, query or fragment."
                )
            if (
                path[0] == "endpoints"
                and path[-1] == "key_env"
                and value
                and not ENV_NAME.fullmatch(value)
            ):
                raise SettingsError(
                    "key_env must name an environment variable, never contain a key."
                )
        changes = {path: value for path, value in proposed.items() if flat.get(path) != value}
        try:
            candidate = _merge(text, changes) if changes else text
        except (StopIteration, TypeError, AttributeError, ValueError) as exc:
            raise SettingsError(
                "This TOML layout cannot be safely edited here; edit attache.toml."
            ) from exc
        if len(candidate.encode("utf-8")) > MAX_BYTES:
            raise SettingsError("Settings files are limited to 64 KB.", 413)
        _validate(_parse(candidate, self.path.parent))
        return candidate

    def _atomic_write(self, path: Path, data: bytes) -> None:
        self._read(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".settings-", delete=False
            ) as out:
                temporary = Path(out.name)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def update(self, payload: dict[str, Any], *, save: bool = False) -> dict[str, Any]:
        """Validate or save a revision-matched proposal, retaining one recoverable backup."""
        try:
            lock_fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise SettingsError("Another settings edit is in progress; try again.", 409) from exc
        try:
            data = self._read(self.path)
            candidate = self._candidate(data, payload)
            before = _flatten(_projection(_parse(self._text(data), self.path.parent)))
            after = _flatten(_projection(_parse(candidate, self.path.parent)))
            changes = [
                {"path": ".".join(path), "before": before.get(path), "after": after.get(path)}
                for path in sorted(before.keys() | after.keys())
                if before.get(path) != after.get(path)
            ]
            if payload.get("restore"):
                raw_before = _flatten(tomllib.loads(self._text(data)))
                raw_after = _flatten(tomllib.loads(candidate))
                hidden = (raw_before.keys() | raw_after.keys()) - (before.keys() | after.keys())
                if any(raw_before.get(path) != raw_after.get(path) for path in hidden):
                    changes.append(
                        {
                            "path": "file-managed settings",
                            "before": "current file",
                            "after": "backup file",
                        }
                    )
            if save and candidate != self._text(data):
                if self._read(self.path) != data:
                    raise SettingsError("Settings changed on disk. Reload them before saving.", 409)
                self._atomic_write(self.backup, data or self._text(data).encode("utf-8"))
                self._atomic_write(self.path, candidate.encode("utf-8"))
            result = self._snapshot(self._read(self.path), None if save else candidate)
            return {**result, "valid": True, "changes": changes}
        finally:
            os.close(lock_fd)
            self.lock.unlink(missing_ok=True)
