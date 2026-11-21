"""`<NAME>_FILE`: secrets read from files (Docker/Swarm secrets, Kubernetes volumes)."""

import pytest

from exuvia.cli import load_secret_files


def test_a_file_sets_the_variable(tmp_path):
    (tmp_path / "token").write_text("s3cret\n", encoding="utf-8")  # `echo` leaves a newline
    env = {"EXUVIA_API_TOKEN_FILE": str(tmp_path / "token")}
    load_secret_files(env)
    assert env["EXUVIA_API_TOKEN"] == "s3cret"


def test_only_secret_settings_read_files(tmp_path):
    (tmp_path / "x").write_text("value", encoding="utf-8")
    env = {"EXUVIA_MODEL_FILE": str(tmp_path / "x")}
    load_secret_files(env)
    assert "EXUVIA_MODEL" not in env


def test_both_set_is_an_error_unless_they_agree(tmp_path):
    (tmp_path / "key").write_text("from-file", encoding="utf-8")
    env = {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "key"), "ANTHROPIC_API_KEY": "plain"}
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY or ANTHROPIC_API_KEY_FILE, not both"):
        load_secret_files(env)
    # Loading twice (the CLI, then a project's .env) is fine.
    env = {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "key")}
    load_secret_files(env)
    load_secret_files(env)
    assert env["ANTHROPIC_API_KEY"] == "from-file"


def test_a_missing_or_empty_file_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="EXUVIA_DB_FILE: cannot read"):
        load_secret_files({"EXUVIA_DB_FILE": str(tmp_path / "nope")})
    (tmp_path / "empty").write_text("\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="is empty"):
        load_secret_files({"EXUVIA_EXECUTOR_TOKEN_FILE": str(tmp_path / "empty")})
