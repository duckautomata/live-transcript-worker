import logging
from unittest.mock import MagicMock, mock_open

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_twitch_lfs import TwitchLFSWorker


@pytest.fixture
def twitch_worker(mocker):
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    mocker.patch("live_transcript_worker.helper.Config")
    worker = TwitchLFSWorker("key", MagicMock(), MagicMock())
    worker.buffer_size_seconds = 6
    # Keep the stale/fall-behind check from tripping during the monitor tests.
    worker.stale_lfs_gap_seconds = 10_000
    return worker


def _drive_monitor(worker, mocker, segment_count):
    """Drive _monitor_segments over `segment_count` always-ready segments."""
    info = StreamInfoObject(url="url", key="key", media_type=Media.AUDIO)
    mocker.patch("live_transcript_worker.worker_twitch_lfs.os.path.exists", return_value=True)
    mocker.patch("live_transcript_worker.worker_twitch_lfs.os.remove")
    mocker.patch("live_transcript_worker.worker_twitch_lfs.open", mock_open(read_data=b"segment-data"), create=True)
    mocker.patch("live_transcript_worker.worker_twitch_lfs.StreamHelper.get_precise_duration", return_value=6.0)
    mocker.patch("live_transcript_worker.worker_twitch_lfs.time.sleep")
    # Pin time so the fall-behind gap check stays well under the threshold.
    mocker.patch("live_transcript_worker.worker_twitch_lfs.time.time", return_value=1000.0)
    worker.stop_event.is_set.side_effect = [False] * segment_count + [True]

    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    # Both upstream processes stay alive so the loop never short-circuits on both_done.
    ytdlp_proc.poll.return_value = None
    ffmpeg_proc.poll.return_value = None

    worker._monitor_segments(info, "/tmp/seg", ytdlp_proc, ffmpeg_proc, 1000.0)


def test_first_segment_callback_fires_exactly_once(twitch_worker, mocker):
    callback = MagicMock()
    twitch_worker._on_first_segment = callback

    _drive_monitor(twitch_worker, mocker, segment_count=3)

    assert twitch_worker.segments_produced == 3
    callback.assert_called_once()


def test_no_callback_when_none_provided(twitch_worker, mocker):
    twitch_worker._on_first_segment = None

    # Should simply not raise when no callback is registered.
    _drive_monitor(twitch_worker, mocker, segment_count=2)

    assert twitch_worker.segments_produced == 2


def test_log_outcome_warns_and_points_at_log_on_error_exit(twitch_worker, caplog):
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 0
    info = StreamInfoObject(url="url", key="key")

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_ytdlp_outcome(info, 1, "/app/tmp/key/ytdlp.log")

    assert "yt-dlp exited (code=1)" in caplog.text
    assert "/app/tmp/key/ytdlp.log" in caplog.text


def test_log_outcome_warns_when_zero_segments_even_on_clean_exit(twitch_worker, caplog):
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 0
    info = StreamInfoObject(url="url", key="key")

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_ytdlp_outcome(info, 0, "/app/tmp/key/ytdlp.log")

    assert "0 segment(s)" in caplog.text


def test_log_outcome_silent_on_healthy_run(twitch_worker, caplog):
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 42
    info = StreamInfoObject(url="url", key="key")

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_ytdlp_outcome(info, 0, "/app/tmp/key/ytdlp.log")

    assert "yt-dlp exited" not in caplog.text


def test_log_outcome_silent_during_shutdown(twitch_worker, caplog):
    # Worker was terminated by us on shutdown: negative exit code, but not a failure.
    twitch_worker.stop_event.is_set.return_value = True
    twitch_worker.segments_produced = 0
    info = StreamInfoObject(url="url", key="key")

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_ytdlp_outcome(info, -15, "/app/tmp/key/ytdlp.log")

    assert "yt-dlp exited" not in caplog.text
