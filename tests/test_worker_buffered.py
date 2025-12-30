import subprocess
from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, StreamInfoObject
from src.live_transcript_worker.worker_buffered import MPEGBufferedWorker


@pytest.fixture
def buffered_worker(mocker):
    mocker.patch("src.live_transcript_worker.worker_abstract.Config")
    queue = MagicMock()
    stop_event = MagicMock()

    worker = MPEGBufferedWorker("key", queue, stop_event)
    worker.buffer_size_seconds = 6
    # Avoid real thread start in start() by mocking Thread
    mocker.patch("threading.Thread")
    return worker


def test_start(buffered_worker, mocker):
    # Mock buffer content injection
    # valid buffer needs to be created by start() first.
    # We can inject it by making stop_event.is_set return False (run loop), then side effect to populate buffer, then False (process), then True (stop).

    mocker.patch.object(buffered_worker, "downloader")

    # Patch Lock to inject data when entered
    mock_lock = MagicMock()
    mock_lock.__enter__.side_effect = lambda: buffered_worker.buffer.extend(b"\x00" * 200000)
    mocker.patch("src.live_transcript_worker.worker_buffered.Lock", return_value=mock_lock)

    buffered_worker.stop_event.is_set.side_effect = [False, True]  # Run once then stop
    buffered_worker.ytdlp_stopped = MagicMock()
    buffered_worker.ytdlp_stopped.is_set.return_value = False

    mocker.patch("src.live_transcript_worker.worker_buffered.StreamHelper.get_duration", return_value=10.0)

    info = StreamInfoObject(url="url", key="key", media_type=Media.AUDIO)

    # We mock Thread so downloader won't actually run, but we simulate buffer filling above
    buffered_worker.start(info)

    assert buffered_worker.queue.put.called


def test_downloader_success(buffered_worker, mocker):
    process = MagicMock()
    process.stdout.read.side_effect = [b"data", b""]  # data then EOF
    process.stderr.read.return_value = b""
    process.returncode = 0
    process.poll.return_value = 0

    mocker.patch.object(buffered_worker, "create_process", return_value=process)

    info = StreamInfoObject(url="url", key="key")
    buffered_worker.stop_event.is_set.return_value = False
    buffered_worker.buffer_lock = MagicMock()
    buffered_worker.buffer = bytearray()
    buffered_worker.ytdlp_stopped = MagicMock()

    buffered_worker.downloader(info)

    assert len(buffered_worker.buffer) == 4  # b"data"


def test_create_process(buffered_worker, mocker):
    mocker.patch("subprocess.Popen")
    buffered_worker.ytdlp_path = "yt-dlp"
    info = StreamInfoObject(url="url", key="key")

    buffered_worker.create_process(info)
    subprocess.Popen.assert_called_once()
