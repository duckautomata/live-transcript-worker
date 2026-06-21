from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import StreamInfoObject
from live_transcript_worker.worker import Worker


@pytest.fixture
def worker(mocker):
    """A Worker with its concrete sub-workers and stream-id persistence mocked, so
    tests can assert how _start_twitch routes without touching subprocesses or disk."""
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    queue = MagicMock()
    stop_event = MagicMock()
    stop_event.is_set.return_value = False
    w = Worker("key", queue, stop_event)

    w.twitch_lfs_worker = MagicMock()
    w.twitch_lfs_worker.segments_produced = 0
    w.twitch_lfs_worker.is_slow = False
    w.live_segment_worker = MagicMock()

    mocker.patch.object(w, "_read_lfs_stream_id", return_value=None)
    mocker.patch.object(w, "_write_lfs_stream_id")
    mocker.patch.object(w, "_lfs_gap_seconds", return_value=600)
    mocker.patch.object(w, "_get_gap_seconds", return_value=0.0)
    return w


def _twitch_info(stream_id="123"):
    return StreamInfoObject(url="https://www.twitch.tv/x", key="key", stream_id=stream_id, start_time="1000")


def _lfs_captures(worker, segments=3, is_slow=False):
    """Make the mocked TwitchLFSWorker behave like a run that produced `segments`
    segments: it fires the on_first_segment callback (as the real one does on its
    first segment) and reports the outcome attributes."""

    def fake_start(info, on_first_segment=None):
        if on_first_segment is not None:
            on_first_segment()
        worker.twitch_lfs_worker.segments_produced = segments
        worker.twitch_lfs_worker.is_slow = is_slow

    worker.twitch_lfs_worker.start.side_effect = fake_start


def test_lfs_success_persists_id_and_does_not_fall_back(worker):
    _lfs_captures(worker, segments=3)

    worker._start_twitch(_twitch_info())

    # id persisted via the first-segment callback; no live-edge fallback.
    worker._write_lfs_stream_id.assert_called_once_with("123")
    worker.live_segment_worker.start.assert_not_called()


def test_lfs_no_segments_falls_back_without_persisting_id(worker):
    info = _twitch_info()
    # Default mock: start() is a no-op, segments_produced stays 0, callback never fires.

    worker._start_twitch(info)

    worker.live_segment_worker.start.assert_called_once_with(info)
    # Not persisting the id is what keeps LFS retryable on a later attempt.
    worker._write_lfs_stream_id.assert_not_called()


def test_lfs_no_segments_does_not_fall_back_during_shutdown(worker):
    worker.stop_event.is_set.return_value = True

    worker._start_twitch(_twitch_info())

    worker.live_segment_worker.start.assert_not_called()


def test_lfs_slow_switches_to_live_segment(worker):
    info = _twitch_info()
    _lfs_captures(worker, segments=5, is_slow=True)

    worker._start_twitch(info)

    # Captured from start (id persisted) then fell behind -> live-edge fallback.
    worker._write_lfs_stream_id.assert_called_once_with("123")
    worker.live_segment_worker.start.assert_called_once_with(info)
    assert worker.twitch_lfs_worker.is_slow is False


def test_restart_on_same_stream_id_uses_live_segment(worker, mocker):
    mocker.patch.object(worker, "_read_lfs_stream_id", return_value="123")
    info = _twitch_info("123")

    worker._start_twitch(info)

    worker.live_segment_worker.start.assert_called_once_with(info)
    worker.twitch_lfs_worker.start.assert_not_called()


def test_gap_too_large_uses_live_segment_and_persists_id(worker, mocker):
    mocker.patch.object(worker, "_get_gap_seconds", return_value=700.0)  # > 600 threshold
    info = _twitch_info()

    worker._start_twitch(info)

    worker.live_segment_worker.start.assert_called_once_with(info)
    # Deliberate downgrade is persisted so a restart short-circuits to live-edge.
    worker._write_lfs_stream_id.assert_called_once_with("123")
    worker.twitch_lfs_worker.start.assert_not_called()
