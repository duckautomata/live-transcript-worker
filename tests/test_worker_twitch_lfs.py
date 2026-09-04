import logging
import os
from unittest.mock import MagicMock, mock_open

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_twitch_lfs import TwitchLFSWorker

PIPELINE = "live_transcript_worker.worker_segment_pipeline"
MODULE = "live_transcript_worker.worker_twitch_lfs"


@pytest.fixture
def twitch_worker(mocker):
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    mocker.patch("live_transcript_worker.helper.Config")
    worker = TwitchLFSWorker("key", MagicMock(), MagicMock())
    worker.buffer_size_seconds = 6
    # Keep the stale/fall-behind checks from tripping during the monitor tests.
    worker.stale_lfs_gap_seconds = 10_000
    worker.stale_ytdlp_seconds = 180
    return worker


def _info(**overrides):
    fields = {
        "url": "https://www.twitch.tv/x",
        "key": "key",
        "stream_id": "1",
        "start_time": "1000",
        "media_type": Media.AUDIO,
    }
    fields.update(overrides)
    return StreamInfoObject(**fields)


def _drive_monitor(worker, mocker, segment_count, audio_start_time=1000.0):
    """Drive _monitor_segments over `segment_count` always-ready segments and
    return the ProcessObjects that were queued."""
    mocker.patch(f"{PIPELINE}.os.path.exists", return_value=True)
    mocker.patch(f"{PIPELINE}.os.remove")
    mocker.patch(f"{PIPELINE}.open", mock_open(read_data=b"segment-data"), create=True)
    mocker.patch(f"{PIPELINE}.os.fstat", return_value=MagicMock(st_mtime=1000.0))
    mocker.patch(f"{PIPELINE}.StreamHelper.get_precise_duration", return_value=6.0)
    mocker.patch(f"{PIPELINE}.StreamHelper.describe_codecs", return_value="audio=aac")
    mocker.patch(f"{PIPELINE}.time.sleep")
    # Pin time so the fall-behind gap check stays well under the threshold.
    mocker.patch(f"{PIPELINE}.time.time", return_value=1000.0)
    mocker.patch(f"{MODULE}.time.time", return_value=1000.0)
    worker.stop_event.is_set.side_effect = [False] * segment_count + [True]
    worker._audio_start_time = audio_start_time

    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    # Both upstream processes stay alive so the loop never short-circuits on both_done.
    ytdlp_proc.poll.return_value = None
    ffmpeg_proc.poll.return_value = None

    worker._monitor_segments(_info(), "/tmp/seg", ytdlp_proc, ffmpeg_proc)

    return [c.args[0] for c in worker.queue.put.call_args_list]


# ----------------------------------------------------------------------
# Segments: timestamps and the first-segment callback
# ----------------------------------------------------------------------


def test_segments_are_timestamped_from_start_time_plus_accumulated_duration(twitch_worker, mocker):
    items = _drive_monitor(twitch_worker, mocker, segment_count=3, audio_start_time=1000.0)

    assert [it.audio_start_time for it in items] == pytest.approx([1000.0, 1006.0, 1012.0])
    assert all(it.vod_accurate for it in items)
    assert twitch_worker.segments_produced == 3


def test_first_segment_callback_fires_exactly_once_on_the_first_segment(twitch_worker, mocker):
    """The callback persists the stream id so a crash after the first segment does
    not replay the stream from the start on restart. It must therefore fire when
    exactly one segment has been counted, and never again."""
    seen = []
    twitch_worker._on_first_segment = lambda: seen.append(twitch_worker.segments_produced)

    _drive_monitor(twitch_worker, mocker, segment_count=3)

    assert twitch_worker.segments_produced == 3
    assert seen == [1]


def test_first_segment_callback_fires_on_a_single_segment_run(twitch_worker, mocker):
    seen = []
    twitch_worker._on_first_segment = lambda: seen.append(twitch_worker.segments_produced)

    _drive_monitor(twitch_worker, mocker, segment_count=1)

    assert seen == [1]


def test_no_callback_when_none_provided(twitch_worker, mocker):
    twitch_worker._on_first_segment = None

    # Should simply not raise when no callback is registered.
    _drive_monitor(twitch_worker, mocker, segment_count=2)

    assert twitch_worker.segments_produced == 2


def test_failing_callback_is_logged_and_does_not_abort_the_run(twitch_worker, mocker, caplog):
    twitch_worker._on_first_segment = MagicMock(side_effect=RuntimeError("disk full"))

    with caplog.at_level(logging.WARNING):
        items = _drive_monitor(twitch_worker, mocker, segment_count=2)

    assert len(items) == 2
    assert "on_first_segment callback failed" in caplog.text


# ----------------------------------------------------------------------
# Falling behind live
# ----------------------------------------------------------------------


def test_poll_hook_switches_to_live_edge_when_too_far_behind(twitch_worker, mocker, caplog):
    mocker.patch(f"{MODULE}.time.time", return_value=1000.0)
    twitch_worker.stale_lfs_gap_seconds = 600
    twitch_worker._audio_start_time = 300.0  # 700 s behind

    with caplog.at_level(logging.WARNING):
        assert twitch_worker._poll_hook(_info()) is True

    assert twitch_worker.is_slow is True
    assert "Switching to LiveSegmentWorker" in caplog.text


def test_poll_hook_keeps_going_within_threshold(twitch_worker, mocker):
    mocker.patch(f"{MODULE}.time.time", return_value=1000.0)
    twitch_worker.stale_lfs_gap_seconds = 600
    twitch_worker._audio_start_time = 500.0  # 500 s behind

    assert twitch_worker._poll_hook(_info()) is False
    assert twitch_worker.is_slow is False


def test_monitor_ends_run_when_poll_hook_trips(twitch_worker, mocker):
    twitch_worker.stale_lfs_gap_seconds = 1

    items = _drive_monitor(twitch_worker, mocker, segment_count=3, audio_start_time=0.0)

    assert items == []
    assert twitch_worker.is_slow is True


# ----------------------------------------------------------------------
# Run setup
# ----------------------------------------------------------------------


def test_begin_run_uses_stream_start_time_or_falls_back_to_now(twitch_worker, mocker, caplog):
    mocker.patch(f"{MODULE}.time.time", return_value=5000.0)

    twitch_worker._begin_run(_info(start_time="1234.5"))
    assert twitch_worker._audio_start_time == 1234.5

    with caplog.at_level(logging.WARNING):
        twitch_worker._begin_run(_info(start_time="not-a-number"))
    assert twitch_worker._audio_start_time == 5000.0
    assert "Invalid start_time" in caplog.text


def test_ytdlp_command_uses_live_from_start_and_filter_free_selector(twitch_worker, mocker):
    popen = mocker.patch(f"{PIPELINE}.subprocess.Popen")
    log_file = MagicMock()

    proc = twitch_worker._create_ytdlp_process(_info(media_type=Media.VIDEO), log_file)

    assert proc is popen.return_value
    cmd = popen.call_args.args[0]
    assert "--live-from-start" in cmd
    fmt = cmd[cmd.index("-f") + 1]
    assert fmt == "b/bv+ba"
    assert "[" not in fmt
    assert cmd[cmd.index("-S") + 1] == "vcodec:h264,proto:m3u8,acodec:aac"
    assert "--no-progress" in cmd
    assert "--quiet" not in cmd
    assert "--no-warnings" not in cmd
    assert cmd[-3:] == ["-o", "-", "https://www.twitch.tv/x"]
    assert popen.call_args.kwargs["stderr"] is log_file
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")


def test_start_wires_pipeline_and_stores_callback(twitch_worker, mocker):
    """start() must reset per-run state, keep the caller's first-segment callback,
    hand ffmpeg the pipe and drop our copy of it (so a dead ffmpeg surfaces as
    EPIPE in yt-dlp instead of yt-dlp blocking forever)."""
    mocker.patch(f"{PIPELINE}.os.path.exists", return_value=False)
    mocker.patch(f"{PIPELINE}.os.makedirs")
    mocker.patch(f"{PIPELINE}.shutil.rmtree")
    log_file = MagicMock()
    mocker.patch(f"{PIPELINE}.StreamHelper.open_process_log", return_value=log_file)
    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = 0
    ffmpeg_proc.poll.return_value = 0
    mocker.patch.object(twitch_worker, "_create_ytdlp_process", return_value=ytdlp_proc)
    create_ffmpeg = mocker.patch.object(twitch_worker, "_create_ffmpeg_process", return_value=ffmpeg_proc)
    mocker.patch.object(twitch_worker, "_monitor_segments")
    outcome = mocker.patch.object(twitch_worker, "_log_outcome")
    # Stale values from a previous run: a fall-behind that tripped before the
    # first segment leaves is_slow set, and it must not colour this run's outcome.
    twitch_worker.segments_produced = 5
    twitch_worker.is_slow = True
    callback = MagicMock()

    twitch_worker.start(_info(start_time="1000"), on_first_segment=callback)

    assert twitch_worker.segments_produced == 0
    assert twitch_worker.is_slow is False
    assert twitch_worker._on_first_segment is callback
    assert twitch_worker._audio_start_time == 1000.0
    assert create_ffmpeg.call_args.args[2] is ytdlp_proc.stdout
    ytdlp_proc.stdout.close.assert_called_once()
    log_file.close.assert_called_once()
    # The run log lives in the worker's own file.
    assert outcome.call_args.args[3].endswith("/tmp/key/twitch_lfs.log")


# ----------------------------------------------------------------------
# Outcome reporting
# ----------------------------------------------------------------------


def test_log_outcome_warns_and_points_at_log_when_lfs_captures_nothing(twitch_worker, mocker, caplog):
    """A fast --live-from-start failure (e.g. VODs disabled) must not be invisible."""
    mocker.patch.object(twitch_worker, "_append_log")
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 0

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_outcome(_info(), 1, 1, "/app/tmp/key/twitch_lfs.log")

    assert "[key][TwitchLFSWorker] Run produced no segments" in caplog.text
    assert "yt-dlp exit=1" in caplog.text
    assert "/app/tmp/key/twitch_lfs.log" in caplog.text


def test_log_outcome_reports_switch_to_live_edge_as_info(twitch_worker, mocker, caplog):
    mocker.patch.object(twitch_worker, "_append_log")
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 42
    twitch_worker.is_slow = True

    with caplog.at_level(logging.INFO):
        twitch_worker._log_outcome(_info(), None, None, "/app/tmp/key/twitch_lfs.log")

    assert "Stopped to switch to the live edge" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_log_outcome_silent_on_healthy_run(twitch_worker, mocker, caplog):
    mocker.patch.object(twitch_worker, "_append_log")
    twitch_worker.stop_event.is_set.return_value = False
    twitch_worker.segments_produced = 42

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_outcome(_info(), 0, 0, "/app/tmp/key/twitch_lfs.log")

    assert "yt-dlp exit" not in caplog.text


def test_log_outcome_silent_during_shutdown(twitch_worker, mocker, caplog):
    mocker.patch.object(twitch_worker, "_append_log")
    # Worker was stopped by us on shutdown: not a failure.
    twitch_worker.stop_event.is_set.return_value = True
    twitch_worker.segments_produced = 0

    with caplog.at_level(logging.WARNING):
        twitch_worker._log_outcome(_info(), -2, -15, "/app/tmp/key/twitch_lfs.log")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
