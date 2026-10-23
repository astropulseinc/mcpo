import asyncio
import json
import os
import tempfile
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from mcpo import mcp_management
from mcpo.hot_reload import MCPHotReloader
from mcpo.main import build_app
from mcpo.mcp_management import validate_server_config


def test_validate_server_config_sse():
    """A URL server needs no template."""
    config = {"type": "sse", "url": "http://example.com/sse"}
    assert validate_server_config("test_server", config) is None


def test_validate_server_config_stdio_needs_a_verified_template(monkeypatch):
    """A command server that matches no verified template is refused."""
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", dict)
    config = {"command": "echo", "args": ["hello", "world"]}
    assert "verified marketplace template" in validate_server_config("test_server", config)


def test_validate_server_config_stdio_matching_a_template(monkeypatch):
    templates = {"echo": {"config_template": {"command": "echo", "args": ["hello"], "env": {}}}}
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", lambda: templates)
    assert validate_server_config("test_server", {"command": "echo", "args": ["hello"]}) is None


@pytest.mark.parametrize("server_id", ["bad.id", "", "a" * 65, "reload", "docs"])
def test_validate_server_config_invalid_id(server_id):
    assert validate_server_config(server_id, {"type": "sse", "url": "http://a/"})


def write_config(data):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        return f.name


def test_load_config_valid():
    """The hot reloader loads a valid config file."""
    config_data = {"mcpServers": {"test_server": {"type": "sse", "url": "http://a/"}}}
    config_path = write_config(config_data)
    try:
        assert MCPHotReloader(FastAPI(), config_path)._load_config() == config_data
    finally:
        os.unlink(config_path)


def test_load_config_invalid_json():
    """Invalid JSON loads as an empty server list instead of crashing the reloader."""
    config_path = write_config('{"invalid": json}')
    try:
        assert MCPHotReloader(FastAPI(), config_path)._load_config() == {"mcpServers": {}}
    finally:
        os.unlink(config_path)


def test_load_config_missing_servers():
    """Starting from a config without mcpServers fails."""
    config_path = write_config({"other": "data"})
    try:

        async def scenario():
            build_app(api_key="k", cors_allow_origins=[], config_path=config_path)

        with pytest.raises(ValueError, match="No 'mcpServers' found"):
            asyncio.run(scenario())
    finally:
        os.unlink(config_path)


def test_reload_starts_added_and_stops_removed_servers():
    """A config change stops removed servers and starts added ones."""
    old = {"mcpServers": {"old_server": {"type": "sse", "url": "http://old/"}}}
    new = {"mcpServers": {"new_server": {"type": "sse", "url": "http://new/"}}}
    config_path = write_config(new)
    try:
        reloader = MCPHotReloader(FastAPI(), config_path)
        reloader.current_config = old
        started, stopped = [], []

        async def start(server_id, config):
            started.append((server_id, config))

        async def stop(server_id):
            stopped.append(server_id)

        reloader._start_server = start
        reloader._stop_server = stop
        asyncio.run(reloader._reload_servers())

        assert stopped == ["old_server"]
        assert started == [("new_server", new["mcpServers"]["new_server"])]
        assert reloader.current_config == new
    finally:
        os.unlink(config_path)


def test_config_watcher_initialization():
    """ConfigWatcher can be initialized."""
    from mcpo.utils.config_watcher import ConfigWatcher

    callback = Mock()
    config_path = write_config({"mcpServers": {}})
    try:
        watcher = ConfigWatcher(config_path, callback)
        assert watcher.config_path.name == os.path.basename(config_path)
        assert watcher.reload_callback == callback
    finally:
        os.unlink(config_path)
