from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, StreamInfoObject
from src.live_transcript_worker.worker_fixedbitrate import MPEGFixedBitrateWorker


@pytest.fixture
def fixed_worker(mocker):
    mocker.patch("src.live_transcript_worker.worker_abstract.Config")
    queue = MagicMock()
    stop_event = MagicMock()
    worker = MPEGFixedBitrateWorker("key", queue, stop_event)
    worker.buffer_size_seconds = 6  # Set explicit int value
    return worker


def test_start(fixed_worker, mocker):
    process = MagicMock()
    process.stdout.read.side_effect = [b"a" * 100000, b""]
    process.poll.return_value = 0
    process.returncode = 0
    process.stderr.read.return_value = b""

    mocker.patch.object(fixed_worker, "create_process", return_value=(process, 48000))
    fixed_worker.stop_event.is_set.side_effect = [False, True]

    info = StreamInfoObject(url="url", key="key", media_type=Media.AUDIO)
    fixed_worker.start(info)

    assert fixed_worker.queue.put.called


def test_create_process(fixed_worker, mocker):
    mocker.patch("subprocess.Popen")
    info = StreamInfoObject(url="http://youtube.com", key="key")

    proc, rate = fixed_worker.create_process(info)
    assert rate == fixed_worker.yt_audio_rate

    info_twitch = StreamInfoObject(url="http://twitch.tv", key="key")
    proc, rate = fixed_worker.create_process(info_twitch)
    assert rate == fixed_worker.twitch_audio_rate
