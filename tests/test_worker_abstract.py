from threading import Event
from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.worker_abstract import AbstractWorker


# Concrete implementation for testing
class ConcreteWorker(AbstractWorker):
    def start(self, info):
        pass


@pytest.fixture
def abstract_worker():
    return ConcreteWorker(key="key", queue=MagicMock(), stop_event=Event())


def test_initialization(abstract_worker):
    assert abstract_worker.key == "key"
    assert abstract_worker.stop_event is not None
    assert abstract_worker.queue is not None


def test_ytdlp_path_resolution(abstract_worker, mocker):
    # Check that ytdlp_path is set correctly (relative to module)
    # Since we can't easily check absolute path, just check it ends with bin/yt-dlp
    assert abstract_worker.ytdlp_path.endswith("bin/yt-dlp")


def test_rates_initialized(abstract_worker):
    assert abstract_worker.yt_audio_rate == 20_000
    assert abstract_worker.twitch_audio_rate == 25_540
