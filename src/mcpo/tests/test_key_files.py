import json

import pytest
from typer.testing import CliRunner

import mcpo
import mcpo.main


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    async def fake_run(host, port, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(mcpo.main, "run", fake_run)
    return seen


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_keys_can_come_from_files_not_argv(tmp_path, captured):
    config = write(tmp_path, "config.json", json.dumps({"mcpServers": {}}))
    key = write(tmp_path, "key", "management-key\n")
    tool_key = write(tmp_path, "tool-key", "tool-key\n")

    result = CliRunner().invoke(
        mcpo.app, ["--api-key-file", key, "--tool-api-key-file", tool_key, "--config", config]
    )

    assert result.exit_code == 0, result.output
    assert captured["api_key"] == "management-key"
    assert captured["tool_api_key"] == "tool-key"


def test_an_empty_key_file_is_refused(tmp_path, captured):
    config = write(tmp_path, "config.json", json.dumps({"mcpServers": {}}))
    key = write(tmp_path, "key", "\n")

    result = CliRunner().invoke(mcpo.app, ["--api-key-file", key, "--config", config])

    assert result.exit_code != 0
    assert captured == {}
