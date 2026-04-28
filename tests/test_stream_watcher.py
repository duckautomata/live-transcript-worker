from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.stream_watcher import StreamWatcher


def _capture_debug_logs(mocker) -> list[str]:
    logs: list[str] = []
    mocker.patch.object(
        __import__("live_transcript_worker.stream_watcher", fromlist=["logger"]).logger,
        "debug",
        side_effect=lambda msg, *a, **k: logs.append(msg),
    )
    return logs


@pytest.fixture
def stream_watcher(mocker, mock_config, mock_storage):
    mocker.patch("live_transcript_worker.stream_watcher.Config", mock_config)
    mocker.patch("live_transcript_worker.stream_watcher.Storage", return_value=mock_storage)
    sw = StreamWatcher()
    sw.stop_event = MagicMock()
    return sw


def test_add(stream_watcher):
    stream_watcher.add("key", ["url1"])
    assert len(stream_watcher.watcher_threads) == 1


def test_start_stop(stream_watcher, mocker):
    stream_watcher.add("key", ["url"])
    mocker.patch("time.sleep")  # Should sleep 1.2
    mocker.patch.object(stream_watcher.process_thread, "start")
    for t in stream_watcher.watcher_threads:
        mocker.patch.object(t, "start")
        mocker.patch.object(t, "join")
        mocker.patch.object(t, "is_alive", return_value=True)  # for stop join

    stream_watcher.ready_event.set()
    stream_watcher.start()

    assert stream_watcher.process_thread.start.called

    stream_watcher.stop()
    assert stream_watcher.stop_event.is_set()


def test_watcher_loop(stream_watcher, mocker):
    # Mock checks
    mocker.patch("time.sleep")
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=True, stream_id="id", start_time="100"),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )

    mock_worker_cls = mocker.patch("live_transcript_worker.stream_watcher.Worker")
    mock_worker = mock_worker_cls.return_value

    stream_watcher.stop_event.is_set.side_effect = [False, True]

    stream_watcher.watcher("key", ["url"])

    mock_worker.start.assert_called()
    stream_watcher.storage.activate.assert_called()
    stream_watcher.storage.deactivate.assert_called()


def test_watcher_skips_worker_for_scheduled_stream(stream_watcher, mocker):
    import time as _time

    mocker.patch("time.sleep")
    future_ts = _time.time() + 3600
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=future_ts),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )

    mock_worker_cls = mocker.patch("live_transcript_worker.stream_watcher.Worker")
    mock_worker = mock_worker_cls.return_value

    stream_watcher.stop_event.is_set.side_effect = [False, True]

    stream_watcher.watcher("key", ["url"])

    mock_worker.start.assert_not_called()
    stream_watcher.storage.activate.assert_not_called()


def test_watcher_extends_next_check_for_scheduled_url(stream_watcher, mocker):
    """A url with a scheduled stream should be logged as having its next check
    pushed out close to the scheduled start (minus the buffer)."""
    mocker.patch("time.sleep")
    # First time.time() seeds next_url_checks via min(); subsequent calls return a
    # value strictly greater so the iteration body actually runs.
    time_iter = iter([1_000_000.0] + [1_000_001.0] * 50)
    mocker.patch("time.time", side_effect=lambda: next(time_iter))
    mocker.patch("random.randint", return_value=0)

    # Schedule far enough out that pre_stream > default retry but still under the cap.
    seconds_until_stream = stream_watcher.pre_scheduled_buffer_seconds + 1800
    scheduled = 1_000_001.0 + seconds_until_stream
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=scheduled),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # is_set: 1 at while-top + 1 in for-loop, then exit.
    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    debug_logs = _capture_debug_logs(mocker)

    stream_watcher.watcher("key", ["yt-url"])

    expected_wait = seconds_until_stream - stream_watcher.pre_scheduled_buffer_seconds
    matches = [m for m in debug_logs if "scheduled stream" in m]
    assert matches, f"no schedule log found: {debug_logs}"
    assert f"Next check in {StreamHelper.format_duration(expected_wait)}" in matches[0], matches[0]


def test_watcher_caps_wait_at_max_seconds(stream_watcher, mocker):
    """When a scheduled stream is far in the future, the next check must be
    capped at max_retry_interval_seconds so we don't miss streams that
    get created or rescheduled while we're sleeping."""
    mocker.patch("time.sleep")
    time_iter = iter([1_000_000.0] + [1_000_001.0] * 50)
    mocker.patch("time.time", side_effect=lambda: next(time_iter))
    mocker.patch("random.randint", return_value=0)

    cap = stream_watcher.max_retry_interval_seconds
    scheduled_far = 1_000_001.0 + cap + 3600  # well beyond the cap
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=scheduled_far),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    debug_logs = _capture_debug_logs(mocker)

    stream_watcher.watcher("key", ["yt-url"])

    matches = [m for m in debug_logs if "scheduled stream" in m]
    assert matches, f"no schedule log found: {debug_logs}"
    assert f"Next check in {StreamHelper.format_duration(cap)}" in matches[0], matches[0]


def test_watcher_uses_max_wait_for_confirmed_offline_url(stream_watcher, mocker):
    """A url confirmed offline (yt-dlp said 'channel is not currently live') with no
    scheduled stream should sleep for max_retry_interval_seconds instead of the default retry."""
    mocker.patch("time.sleep")
    time_iter = iter([1_000_000.0] + [1_000_001.0] * 50)
    mocker.patch("time.time", side_effect=lambda: next(time_iter))
    mocker.patch("random.randint", return_value=0)

    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, confirmed_offline=True),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    debug_logs = _capture_debug_logs(mocker)

    stream_watcher.watcher("key", ["yt-url"])

    cap = stream_watcher.max_retry_interval_seconds
    matches = [m for m in debug_logs if "offline with no schedule" in m]
    assert matches, f"no offline log found: {debug_logs}"
    assert f"Next check in {StreamHelper.format_duration(cap)}" in matches[0], matches[0]


def test_watcher_polls_each_url_independently(stream_watcher, mocker):
    """Mixed YouTube + Twitch in the same key: both must be polled in iter 1, and
    only the YouTube url with a schedule gets the extended wait. The Twitch url
    keeps the default retry."""
    mocker.patch("time.sleep")
    time_iter = iter([1_000_000.0] + [1_000_001.0] * 50)
    mocker.patch("time.time", side_effect=lambda: next(time_iter))
    mocker.patch("random.randint", return_value=0)

    yt_info = StreamInfoObject(is_live=False, scheduled_start_time=1_000_001.0 + 1800)
    twitch_info = StreamInfoObject(is_live=False, scheduled_start_time=0.0)
    mock_stats = mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        side_effect=[yt_info, twitch_info],
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # 1 at while-top + 2 in for-loop, then exit.
    stream_watcher.stop_event.is_set.side_effect = [False, False, False, True]

    debug_logs = _capture_debug_logs(mocker)

    stream_watcher.watcher("key", ["yt-url", "twitch-url"])

    # Both urls polled.
    assert mock_stats.call_count == 2
    # Only the YouTube url logs a schedule message; Twitch hits the default-poll branch.
    schedule_logs = [m for m in debug_logs if "scheduled stream" in m]
    assert len(schedule_logs) == 1
    assert "yt-url" in schedule_logs[0]


def test_processor(stream_watcher, mocker):
    mocker.patch("live_transcript_worker.stream_watcher.ProcessAudio")

    item = MagicMock()
    stream_watcher.processing_queue.put(item)

    stream_watcher.stop_event.set()  # Stop immediately after processing
    # But loop condition is or not empty. So it will process item then stop?
    # condition: not stop OR not empty OR not finished
    # if stop is set, and empty is false (has item), it continues.
    # After get, item is processed. Then loop again.
    # We need to make queue empty eventually.

    # run in thread or just call method? Method is blocking loop.
    # We control loop with side effects or exceptions?
    # Let's rely on stop_event and queue state.

    # We need to ensure loop exits.
    # Loop: while not stop or not empty or not finished.
    # We set stop=True. Queue has 1 item.
    # Iter 1: not stop(False) -> True. Process item. Queue empty? Not yet properly.
    # Queue.get removes it. task_done called.

    stream_watcher.worker_finished_event.set()  # ensure finished is set too

    # We need to make sure `not empty` becomes False.

    stream_watcher.processor()

    assert stream_watcher.processing_queue.empty()
