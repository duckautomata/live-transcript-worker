import sys

import pytest
import yaml

from src.live_transcript_worker.config import Config


# Helper to create a temporary config file
@pytest.fixture
def temp_config_file(tmp_path, mocker):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"

    # Mock where the Config class looks for the file
    # We need to patch the path resolution inside Config.get_config
    # But since Config.get_config uses os.path.dirname(os.path.abspath(__name__)),
    # it's tricky to mock project_root_dir without changing the code or using a sophisticated mock.
    # A cleaner way is to mock open() or mock the return of get_config for consumers.

    # However, to test loading logic itself, we should try to point it to our temp file.
    # We can mock `os.path.abspath` or `os.path.join` but that's risky.
    # Let's inspect config.py again.
    # It does: `project_root_dir = os.path.dirname(os.path.abspath(__name__))`
    # This `__name__` refers to `src.live_transcript_worker.config`.

    # Actually, the Config class is hardcoded to look in relative path based on `__name__`.
    # We can patch `os.path.abspath` to return the parent of our `tmp_path` so that `join(..., "config", ...)` resolves to `tmp_path/config/...`
    # But `__name__` in config.py is just a string.
    # `os.path.abspath(__name__)` where `__name__` is `src.live_transcript_worker.config` ... wait.
    # passing a module name to abspath? That seems wrong in the original code if it meant `__file__`.
    # Let's check the original code: `os.path.dirname(os.path.abspath(__name__))`
    # If `__name__` is "src.live_transcript_worker.config", abspath will try to resolve that file in CWD.

    # If the original code uses `__name__` instead of `__file__`, that might be a bug or I misread it.
    # Let's assume for now we mock `os.path.abspath` to return `str(tmp_path)`.

    return config_file


def test_get_config_no_file(mocker):
    # Simulate file not found
    mocker.patch("builtins.open", side_effect=FileNotFoundError)
    mocker.patch("sys.exit")  # Prevent exit

    config = Config.get_config()
    assert config is None
    # Verify sys.exit was called
    sys.exit.assert_called_with(1)


def test_get_config_invalid_yaml(mocker):
    # Simulate invalid YAML
    mocker.patch("builtins.open", mocker.mock_open(read_data="{ invalid yaml"))
    mocker.patch("yaml.safe_load", side_effect=yaml.YAMLError)
    mocker.patch("sys.exit")

    config = Config.get_config()
    assert config is None
    sys.exit.assert_called_with(1)


def test_get_server_config(mocker):
    mock_data = {"server": {"url": "foo"}}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    server_conf = Config.get_server_config()
    assert server_conf == {"url": "foo"}


def test_get_server_config_empty(mocker):
    mocker.patch.object(Config, "get_config", return_value=None)
    assert Config.get_server_config() == {}


def test_get_transcription_config(mocker):
    mock_data = {"transcription": {"model": "large"}}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    trans_conf = Config.get_transcription_config()
    assert trans_conf == {"model": "large"}


def test_get_all_streamers_config(mocker):
    mock_data = {"streamers": [{"key": "k1"}]}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    assert Config.get_all_streamers_config() == [{"key": "k1"}]


def test_get_streamer_config_found(mocker):
    mock_data = {"streamers": [{"key": "k1", "val": 1}, {"key": "k2", "val": 2}]}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    conf = Config.get_streamer_config("k1")
    assert conf == {"key": "k1", "val": 1}


def test_get_streamer_config_not_found(mocker):
    mock_data = {"streamers": [{"key": "k1"}]}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    conf = Config.get_streamer_config("k2")
    assert conf == {}


def test_get_streamer_config_invalid_list(mocker):
    mock_data = {"streamers": "not-a-list"}
    mocker.patch.object(Config, "get_config", return_value=mock_data)

    assert Config.get_streamer_config("k1") == {}


def test_get_id_blacklist_config(mocker):
    mock_data = {"id_blacklist": ["id1", "id2"]}
    mocker.patch.object(Config, "get_config", return_value=mock_data)
    assert Config.get_id_blacklist_config() == ["id1", "id2"]
