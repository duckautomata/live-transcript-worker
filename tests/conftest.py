import os
import sys

import pytest

# Add src to path so we can import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def mock_config(mocker):
    """Mocks the Config class."""
    mock_conf = mocker.patch("src.live_transcript_worker.config.Config")
    # Default values for common lookups
    mock_conf.get_server_config.return_value = {
        "url": "http://localhost:8080",
        "apiKey": "test-key",
        "enabled": True,
    }
    mock_conf.get_transcription_config.return_value = {"model": "base", "device": "cpu", "compute_type": "int8"}
    mock_conf.get_streamer_config.return_value = {"media_type": "audio"}
    mock_conf.get_id_blacklist_config.return_value = []
    # If get_config is called directly
    mock_conf.get_config.return_value = {
        "server": mock_conf.get_server_config.return_value,
        "transcription": mock_conf.get_transcription_config.return_value,
        "streamers": [],
        "id_blacklist": [],
    }
    return mock_conf


@pytest.fixture
def mock_storage(mocker):
    """Mocks the Storage class."""
    # Since into Storage is a Singleton, we need to be careful.
    # We patch the class itself so new instances are mocks,
    # BUT since it's a singleton, if it was already initialized, we might get the real one.
    # So we patch the methods on the singleton instance if it exists, or patch the class.
    # Easiest is to patch the module where it is used or patch the class methods.

    # We will patch the imported class in the modules where it is used usually.
    # But for a general fixture, let's try to patch the class in storage.py
    return mocker.patch("src.live_transcript_worker.storage.Storage")


@pytest.fixture
def sample_audio():
    """Returns a dummy audio bytes object."""
    return b"\x00" * 1024
