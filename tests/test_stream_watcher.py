from threading import Event
from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.stream_watcher import StreamWatcher, _CompositeStopEvent


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
    # Don't actually spawn background threads (process_thread, watcher threads,
    # restart-poller threads) during tests — they'd race with the test thread on
    # mocked stop_event side_effects. Each Thread() call returns a fresh
    # MagicMock so per-thread attributes like .start() can be set independently.
    mocker.patch(
        "live_transcript_worker.stream_watcher.Thread",
        side_effect=lambda *a, **k: MagicMock(),
    )
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


def test_add_incoming(stream_watcher):
    stream_watcher.add_incoming("doki")
    assert len(stream_watcher.watcher_threads) == 1
    stream_watcher.storage.create_paths.assert_called_with("doki")


def test_watcher_incoming_runs_worker_for_live_stream(stream_watcher, mocker):
    """A URL fetched from /incoming whose stream is live should be activated,
    handed to the worker, then deactivated. We do NOT delete from /incoming
    here — worker.start can exit cleanly or from a transient error, so we let
    the offline-twice path handle cleanup uniformly."""
    mocker.patch("time.sleep")
    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
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

    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    stream_watcher.watcher_incoming("doki")

    mock_worker.start.assert_called()
    stream_watcher.storage.activate.assert_called()
    stream_watcher.storage.deactivate.assert_called()
    stream_watcher.storage.delete_incoming_url.assert_not_called()


def test_watcher_incoming_deletes_after_two_offline_checks(stream_watcher, mocker):
    """A URL whose stream is offline twice in a row should be removed from /incoming.
    The first offline check should NOT trigger a delete."""
    mocker.patch("time.sleep")
    mocker.patch("random.randint", return_value=0)
    # Force the URL to be due on every iteration.
    stream_watcher.retry_interval_seconds = 0
    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=0.0),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # 1st outer: while-top False, then per-URL is_set False after schedule; 2nd outer:
    # while-top False, then per-URL hits the delete branch (continue, no is_set check);
    # 3rd outer: while-top True -> exit.
    stream_watcher.stop_event.is_set.side_effect = [False, False, False, True]

    stream_watcher.watcher_incoming("doki")

    stream_watcher.storage.delete_incoming_url.assert_called_with("doki", "https://twitch.tv/foo")


def test_watcher_incoming_no_delete_after_single_offline(stream_watcher, mocker):
    """A single offline check is not enough to trigger deletion."""
    mocker.patch("time.sleep")
    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=0.0),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # Only one inner iteration - one offline check. Should NOT delete yet.
    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    stream_watcher.watcher_incoming("doki")

    stream_watcher.storage.delete_incoming_url.assert_not_called()


def test_watcher_incoming_skips_scheduled_streams_for_offline_count(stream_watcher, mocker):
    """A YouTube URL with a scheduled_start_time in the future is not 'offline' —
    we wait for the scheduled time. It should never be deleted just because
    successive checks return is_live=False."""
    import time as _time

    mocker.patch("time.sleep")
    stream_watcher.storage.get_incoming_urls.return_value = ["https://youtube.com/watch?v=abc"]
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=_time.time() + 3600),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # Many iterations - none should delete since the stream is scheduled.
    stream_watcher.stop_event.is_set.side_effect = [False] * 10 + [True]

    stream_watcher.watcher_incoming("doki")

    stream_watcher.storage.delete_incoming_url.assert_not_called()


def test_watcher_incoming_dedupes_urls_across_polls(stream_watcher, mocker):
    """If the bot re-queues the same URL while we're still tracking it, the
    second poll should not log/track it as new."""
    mocker.patch("time.sleep")
    # Repeated GETs return the same URL - simulating the bot re-queuing.
    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=0.0),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # Force /incoming to be polled multiple times by setting the intervals to 0
    # (the fallback interval is the one used while events polling is enabled).
    stream_watcher.incoming_poll_interval_seconds = 0
    stream_watcher.events_fallback_interval_seconds = 0
    stream_watcher.stop_event.is_set.side_effect = [False, False, False, False, True]

    info_logs: list[str] = []
    mocker.patch.object(
        __import__("live_transcript_worker.stream_watcher", fromlist=["logger"]).logger,
        "info",
        side_effect=lambda msg, *a, **k: info_logs.append(msg),
    )

    stream_watcher.watcher_incoming("doki")

    # "new incoming URL" should appear only once even though /incoming was polled multiple times.
    new_url_logs = [m for m in info_logs if "new incoming URL" in m]
    assert len(new_url_logs) == 1, new_url_logs


def test_watcher_incoming_deletes_after_worker_then_two_offline(stream_watcher, mocker):
    """Full lifecycle: worker runs against a live stream, exits, then the next
    two stats checks come back offline and the URL gets removed from /incoming."""
    mocker.patch("time.sleep")
    mocker.patch("random.randint", return_value=0)
    stream_watcher.retry_interval_seconds = 0
    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
    # Iteration 1: live -> worker runs. Iteration 2: offline (count=1).
    # Iteration 3: offline (count=2 -> delete).
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        side_effect=[
            StreamInfoObject(is_live=True, stream_id="id", start_time="100"),
            StreamInfoObject(is_live=False, scheduled_start_time=0.0),
            StreamInfoObject(is_live=False, scheduled_start_time=0.0),
        ],
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )

    mock_worker_cls = mocker.patch("live_transcript_worker.stream_watcher.Worker")
    mock_worker = mock_worker_cls.return_value

    # 3 outer iterations + exit. Each iteration: while-top False, per-URL is_set
    # False after schedule. The third iteration deletes via the offline branch
    # (continue, no is_set check), so 5 False reads then a True at the top.
    stream_watcher.stop_event.is_set.side_effect = [False, False, False, False, False, True]

    stream_watcher.watcher_incoming("doki")

    mock_worker.start.assert_called_once()
    stream_watcher.storage.delete_incoming_url.assert_called_once_with("doki", "https://twitch.tv/foo")


def test_composite_stop_event_or():
    a, b = Event(), Event()
    composite = _CompositeStopEvent(a, b)
    assert composite.is_set() is False
    a.set()
    assert composite.is_set() is True
    a.clear()
    b.set()
    assert composite.is_set() is True


def test_watcher_incoming_handles_restart_when_idle(stream_watcher, mocker):
    """If a restart is signaled while the watcher is idle (no live stream),
    the event is cleared on the next loop iteration without calling deactivate
    via the restart-handling path (last_stream_id is empty)."""
    mocker.patch("time.sleep")
    stream_watcher.storage.get_incoming_urls.return_value = []
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # Pre-populate the restart event so the watcher sees it on the first iteration.
    restart_event = Event()
    restart_event.set()
    stream_watcher._restart_events["doki"] = restart_event

    # 1st outer: while-top False, hits restart branch -> continue. 2nd outer: True -> exit.
    stream_watcher.stop_event.is_set.side_effect = [False, True]

    stream_watcher.watcher_incoming("doki")

    # _handle_restart cleared the event and the loop didn't re-enter the body.
    assert restart_event.is_set() is False


def test_watcher_incoming_aborts_running_stream_on_restart(stream_watcher, mocker):
    """The composite stop event passed to Worker must OR stop_event with the
    per-key restart_event — so Worker.is_set() reads True as soon as restart
    fires, even though stop_event is still False. After the worker exits, the
    watcher deactivates the stream and clears the event."""
    mocker.patch("time.sleep")

    restart_event = Event()
    stream_watcher._restart_events["doki"] = restart_event

    stream_watcher.storage.get_incoming_urls.return_value = ["https://twitch.tv/foo"]
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

    def fake_start(info):
        # Simulate the bg poller firing while the worker is running.
        restart_event.set()

    mock_worker.start.side_effect = fake_start

    # 1st outer: while-top False -> run worker (sets restart). break out of for-loop
    # after the live branch, no per-URL is_set check. 2nd outer: while-top False ->
    # hits restart branch, continue. 3rd outer: True -> exit.
    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    stream_watcher.watcher_incoming("doki")

    mock_worker.start.assert_called_once()
    # Verify the composite event passed to Worker is the OR-wrapper. Workers only
    # call is_set() so this guarantees they see restart_event firing.
    composite_stop = mock_worker_cls.call_args.args[2]
    assert isinstance(composite_stop, _CompositeStopEvent)
    # Stream was deactivated at least once via _handle_restart's deactivate call.
    stream_watcher.storage.deactivate.assert_any_call("doki", "id")
    assert restart_event.is_set() is False


def test_watcher_handles_restart_when_idle(stream_watcher, mocker):
    """URL-mode watcher resets its per-URL check times on restart so each URL
    is re-checked immediately."""
    mocker.patch("time.sleep")
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=0.0),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    restart_event = Event()
    restart_event.set()
    stream_watcher._restart_events["doki"] = restart_event

    stream_watcher.stop_event.is_set.side_effect = [False, True]

    stream_watcher.watcher("doki", ["https://twitch.tv/foo"])

    assert restart_event.is_set() is False


def test_restart_poller_sets_event_and_deletes(stream_watcher, mocker):
    """When the server reports a pending restart, the poller sets the local
    event (so the running Worker aborts) and DELETEs the server-side request."""
    restart_event = Event()
    stream_watcher.storage.is_restart_requested.return_value = True
    # Run one iteration of the poller, then exit via wait().
    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True

    stream_watcher._restart_poller("doki", restart_event)

    assert restart_event.is_set() is True
    stream_watcher.storage.delete_restart_request.assert_called_once_with("doki")


def test_restart_poller_skips_when_event_already_set(stream_watcher, mocker):
    """If a previous restart is still being handled (event is set), the poller
    should not re-trigger or wipe a fresh server-side POST."""
    restart_event = Event()
    restart_event.set()
    # The storage call shouldn't even be made.
    stream_watcher.storage.is_restart_requested.side_effect = AssertionError("should not be polled")
    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True

    stream_watcher._restart_poller("doki", restart_event)

    stream_watcher.storage.delete_restart_request.assert_not_called()


def test_restart_poller_no_action_when_not_pending(stream_watcher, mocker):
    """When no restart is pending, the poller waits for the next interval and
    leaves the event alone."""
    restart_event = Event()
    stream_watcher.storage.is_restart_requested.return_value = False
    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True

    stream_watcher._restart_poller("doki", restart_event)

    assert restart_event.is_set() is False
    stream_watcher.storage.delete_restart_request.assert_not_called()


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


def test_events_listener_fans_out_flags(stream_watcher):
    """A poll_events response with both flags must set the key's incoming
    nudge and run the restart check+ack, exactly like the legacy pollers."""
    stream_watcher.add_incoming("doki")
    stream_watcher.storage.poll_events.return_value = ({"doki": ["incoming", "restart"]}, 42)
    stream_watcher.storage.is_restart_requested.return_value = True

    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True  # exit at the post-events pause

    stream_watcher._events_listener(["doki"])

    assert stream_watcher._incoming_events["doki"].is_set()
    assert stream_watcher._restart_events["doki"].is_set()
    stream_watcher.storage.delete_restart_request.assert_called_with("doki")


def test_events_listener_echoes_cursor(stream_watcher):
    """The cursor from one response must be sent as `since` on the next poll
    so already-reported incoming URLs aren't reported again."""
    stream_watcher.add_incoming("doki")
    stream_watcher.storage.poll_events.side_effect = [({}, 42), ({}, 99)]
    stream_watcher.stop_event.is_set.side_effect = [False, False, True]

    stream_watcher._events_listener(["doki"])

    calls = stream_watcher.storage.poll_events.call_args_list
    assert calls[0][0][1] == 0
    assert calls[1][0][1] == 42


def test_events_listener_degrades_when_events_unsupported(stream_watcher):
    """When /events fails (older server, network error) the listener must do
    one legacy polling round — restart check per key plus an incoming nudge —
    and wait the legacy interval before retrying."""
    stream_watcher.add_incoming("doki")
    stream_watcher.storage.poll_events.return_value = None
    stream_watcher.storage.is_restart_requested.return_value = False

    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True  # exit during the interval sleep

    stream_watcher._events_listener(["doki"])

    stream_watcher.storage.is_restart_requested.assert_called_with("doki")
    assert stream_watcher._incoming_events["doki"].is_set()
    stream_watcher.stop_event.wait.assert_called_with(stream_watcher.incoming_poll_interval_seconds)


def test_events_listener_skips_restart_while_already_handling(stream_watcher):
    """A restart flag arriving while the previous restart is still being
    handled (restart_event still set) must not re-trigger or wipe the
    server-side request."""
    stream_watcher.add_incoming("doki")
    stream_watcher._restart_events["doki"].set()
    stream_watcher.storage.poll_events.return_value = ({"doki": ["restart"]}, 1)

    stream_watcher.stop_event.is_set.side_effect = [False, True]
    stream_watcher.stop_event.wait.return_value = True

    stream_watcher._events_listener(["doki"])

    stream_watcher.storage.is_restart_requested.assert_not_called()
    stream_watcher.storage.delete_restart_request.assert_not_called()


def test_watcher_incoming_refreshes_on_event_nudge(stream_watcher, mocker):
    """An incoming_event nudge from the events listener must force an
    immediate /incoming refresh even though the fallback interval is far off."""
    mocker.patch("time.sleep")
    stream_watcher.events_fallback_interval_seconds = 10_000
    incoming_event = stream_watcher._incoming_events.setdefault("doki", Event())
    stream_watcher.storage.get_incoming_urls.side_effect = [[], ["https://twitch.tv/foo"]]
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=False, scheduled_start_time=0.0),
    )
    mocker.patch(
        "live_transcript_worker.helper.StreamHelper.get_media_type",
        return_value=Media.AUDIO,
    )
    mocker.patch("live_transcript_worker.stream_watcher.Worker")

    # Loop passes: 1st fetches the (empty) queue, then the nudge lands during
    # the 2nd while-top check, so the 2nd pass must refetch; stop on the 3rd.
    calls = {"n": 0}

    def is_set_side_effect():
        calls["n"] += 1
        if calls["n"] == 2:
            incoming_event.set()
        return calls["n"] >= 3

    stream_watcher.stop_event.is_set.side_effect = is_set_side_effect

    stream_watcher.watcher_incoming("doki")

    assert stream_watcher.storage.get_incoming_urls.call_count == 2
    assert not incoming_event.is_set(), "nudge must be cleared once consumed"
