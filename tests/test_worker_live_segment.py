import logging
import os
import signal
import subprocess
from unittest.mock import MagicMock, call, mock_open

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_live_segment import LiveSegmentWorker

# The pipeline (process handling, monitor loop) lives in the shared base module;
# LiveSegmentWorker only contributes its timestamping and names.
MODULE = "live_transcript_worker.worker_segment_pipeline"


@pytest.fixture
def segment_worker(mocker):
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    mocker.patch("live_transcript_worker.helper.Config")
    queue = MagicMock()
    stop_event = MagicMock()
    worker = LiveSegmentWorker("key", queue, stop_event)
    worker.buffer_size_seconds = 6
    worker.live_latency_seconds = 1
    worker.stale_ytdlp_seconds = 180
    return worker


def _info(media_type=Media.VIDEO, url="https://www.youtube.com/watch?v=abc"):
    return StreamInfoObject(url=url, key="key", stream_id="abc", media_type=media_type)


def _run_monitor(worker, mocker, mtimes, duration=6.0):
    """Drive _monitor_segments over len(mtimes) always-ready segments, each
    reporting the given file mtime and duration, then return the
    audio_start_time queued for each segment."""
    info = StreamInfoObject(url="url", key="key", media_type=Media.AUDIO)

    # Every segment is "ready"; both processes stay alive so the loop never
    # short-circuits on the both_done checks.
    mocker.patch(f"{MODULE}.os.path.exists", return_value=True)
    mocker.patch(f"{MODULE}.os.remove")
    # `open` is patched only within the worker module's namespace (create=True),
    # so we don't disturb file access elsewhere during the test.
    mocker.patch(f"{MODULE}.open", mock_open(read_data=b"segment-data"), create=True)
    # Each segment's file mtime is what anchors its timestamp. os.fstat is called
    # once per segment, from the open handle.
    mocker.patch(
        f"{MODULE}.os.fstat",
        side_effect=[MagicMock(st_mtime=m) for m in mtimes],
    )
    mocker.patch(f"{MODULE}.StreamHelper.get_precise_duration", return_value=duration)
    mocker.patch(f"{MODULE}.StreamHelper.describe_codecs", return_value="video=h264, audio=aac")
    mocker.patch(f"{MODULE}.time.sleep")
    worker.stop_event.is_set.side_effect = [False] * len(mtimes) + [True]

    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = None
    ffmpeg_proc.poll.return_value = None

    worker._monitor_segments(info, "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    return [call.args[0].audio_start_time for call in worker.queue.put.call_args_list]


# ----------------------------------------------------------------------
# Timestamping
# ----------------------------------------------------------------------


def test_monitor_segments_anchors_each_segment_to_its_mtime(segment_worker, mocker):
    """Each segment is timestamped from its own file mtime, walked back over its
    duration and the platform latency: audio_start_time == mtime - duration - latency."""
    start_times = _run_monitor(segment_worker, mocker, mtimes=[1006.0, 1012.0, 1018.0])

    # mtime - 6.0s duration - 1.0s latency
    assert start_times == pytest.approx([999.0, 1005.0, 1011.0])


def test_monitor_segments_backlog_drain_keeps_distinct_timestamps(segment_worker, mocker):
    """If the monitor falls behind and several segments are ready at once, it
    drains them back-to-back with no delay. Because timestamps come from each
    file's write time (mtime), not from "now", the drained segments keep their
    real, distinct, correctly-spaced timestamps instead of collapsing onto one
    instant (which a `time.time()`-per-segment scheme would do)."""
    # Five segments produced 6s apart, all sitting on disk when the monitor wakes.
    start_times = _run_monitor(segment_worker, mocker, mtimes=[2006.0, 2012.0, 2018.0, 2024.0, 2030.0])

    assert start_times == pytest.approx([1999.0, 2005.0, 2011.0, 2017.0, 2023.0])
    # Distinct and monotonically ~6s apart — not collapsed to a single instant.
    gaps = [b - a for a, b in zip(start_times, start_times[1:], strict=False)]
    assert gaps == pytest.approx([6.0, 6.0, 6.0, 6.0])


def test_monitor_segments_offline_then_online_recovers_to_live(segment_worker, mocker):
    """When the stream goes offline then online, the segment straddling the
    outage is written (and so stamped) after the stream resumes, and every later
    segment carries a fresh post-outage mtime. Timestamps therefore jump forward
    to the new live edge and resume normal spacing — the outage does not
    accumulate as permanent drift the way a running duration total would."""
    start_times = _run_monitor(
        segment_worker,
        mocker,
        # seg0, seg1 normal; ~60s outage; seg2 straddles and finishes post-outage;
        # seg3, seg4 fully post-outage.
        mtimes=[1006.0, 1012.0, 1072.0, 1078.0, 1084.0],
    )

    assert len(start_times) == 5
    # Pre-outage: normal 6s spacing.
    assert start_times[0] == pytest.approx(999.0)
    assert start_times[1] == pytest.approx(1005.0)
    # The outage shows up as a one-segment jump forward to the resumed live edge.
    assert start_times[2] - start_times[1] == pytest.approx(60.0)
    # Recovery is immediate: spacing returns to ~6s, anchored to post-outage time.
    # A running-duration total would have stamped these 1011.0 and 1017.0 —
    # permanently ~60s behind live. mtime keeps them at the real time.
    assert start_times[3] == pytest.approx(1071.0)
    assert start_times[4] == pytest.approx(1077.0)


def test_monitor_drains_remaining_segments_after_both_processes_exit(segment_worker, mocker, tmp_path, caplog):
    """End of stream: both processes are gone and N segments sit on disk. The last
    one has no successor, so it must become readable through both_done, and the
    loop must finish only once no further segment exists."""
    segment_dir = tmp_path / "live_segments"
    segment_dir.mkdir()
    for seq in range(3):
        (segment_dir / f"chunk{seq:06d}.ts").write_bytes(b"segment-%d" % seq)
    mocker.patch(f"{MODULE}.StreamHelper.get_precise_duration", return_value=6.0)
    mocker.patch(f"{MODULE}.StreamHelper.describe_codecs", return_value="audio=aac")
    mocker.patch(f"{MODULE}.time.sleep")
    segment_worker.stop_event.is_set.return_value = False
    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = 0
    ffmpeg_proc.poll.return_value = 0

    with caplog.at_level(logging.INFO):
        segment_worker._monitor_segments(_info(Media.AUDIO), str(segment_dir), ytdlp_proc, ffmpeg_proc)

    assert segment_worker.segments_produced == 3
    assert [c.args[0].raw for c in segment_worker.queue.put.call_args_list] == [b"segment-0", b"segment-1", b"segment-2"]
    assert "Stream ended after 3 segments" in caplog.text
    # Consumed segments are removed from disk.
    assert list(segment_dir.iterdir()) == []


def test_monitor_counts_segments_and_logs_codecs_once(segment_worker, mocker, caplog):
    with caplog.at_level(logging.INFO):
        _run_monitor(segment_worker, mocker, mtimes=[1006.0, 1012.0, 1018.0])

    assert segment_worker.segments_produced == 3
    # The codec summary is a one-off diagnostic for the first usable segment only.
    assert caplog.text.count("First segment ready") == 1
    assert "video=h264, audio=aac" in caplog.text


# ----------------------------------------------------------------------
# yt-dlp / ffmpeg command lines
# ----------------------------------------------------------------------


def test_ytdlp_video_command_has_no_codec_filters_and_prefers_h264_aac(segment_worker, mocker):
    """Regression for "Requested format is not available": YouTube's HLS audio
    renditions report an unknown codec, so any [acodec=...] filter fails outright.
    The selector must stay filter-free and express the codec preference as a sort."""
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=["--match-filter", "x"])
    log_file = MagicMock()
    info = _info(Media.VIDEO)

    proc = segment_worker._create_ytdlp_process(info, log_file)

    assert proc is popen.return_value
    cmd = popen.call_args.args[0]
    fmt = cmd[cmd.index("-f") + 1]
    assert fmt == "b/bv+ba"
    assert "[" not in fmt
    assert cmd[cmd.index("-S") + 1] == "vcodec:h264,proto:m3u8,acodec:aac"
    assert cmd[cmd.index("--downloader-args") + 1] == "ffmpeg:-loglevel warning -nostats"
    # yt-dlp's status lines (selected formats, filter rejections) and warnings are
    # wanted in the log; only the progress bar is suppressed.
    assert "--no-progress" in cmd
    assert "--quiet" not in cmd
    assert "--no-warnings" not in cmd
    assert "--match-filter" in cmd
    assert cmd[-3:] == ["-o", "-", info.url]
    assert popen.call_args.kwargs["stdout"] is subprocess.PIPE
    assert popen.call_args.kwargs["stderr"] is log_file
    # Own process group, so an escalated kill takes yt-dlp's ffmpeg child with it.
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")
    # The command line is recorded ahead of the process's own output.
    written = "".join(c.args[0] for c in log_file.write.call_args_list)
    assert written.startswith("yt-dlp: ")
    assert "b/bv+ba" in written


def test_ytdlp_audio_command_uses_best_audio(segment_worker, mocker):
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=[])

    for media_type in (Media.AUDIO, Media.NONE):
        segment_worker._create_ytdlp_process(_info(media_type), MagicMock())
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("-f") + 1] == "ba/b"
        assert cmd[cmd.index("-S") + 1] == "vcodec:h264,proto:m3u8,acodec:aac"


def test_ytdlp_output_discarded_when_log_unavailable(segment_worker, mocker):
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=[])

    proc = segment_worker._create_ytdlp_process(_info(), None)

    assert proc is popen.return_value
    assert popen.call_args.kwargs["stderr"] is subprocess.DEVNULL


def test_ytdlp_start_failure_returns_none(segment_worker, mocker, caplog):
    mocker.patch(f"{MODULE}.subprocess.Popen", side_effect=FileNotFoundError("yt-dlp"))
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=[])

    with caplog.at_level(logging.ERROR):
        assert segment_worker._create_ytdlp_process(_info(), None) is None

    assert "Failed to start yt-dlp" in caplog.text


def test_ffmpeg_command_logs_warnings_only_to_run_log(segment_worker, mocker):
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    log_file = MagicMock()
    stdin = MagicMock()

    proc = segment_worker._create_ffmpeg_process(_info(), "/tmp/segdir", stdin, log_file)

    assert proc is popen.return_value
    cmd = popen.call_args.args[0]
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-loglevel") + 1] == "warning"
    assert "-nostats" in cmd
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert cmd[cmd.index("-segment_time") + 1] == "6"
    assert cmd[cmd.index("-segment_format") + 1] == "mpegts"
    # Without an explicit PCR period the mpegts muxer warns "frame size not set"
    # once per segment on audio-only input, flooding the run log.
    assert cmd[cmd.index("-segment_format_options") + 1] == "pcr_period=20"
    assert cmd[-1] == "/tmp/segdir/chunk%06d.ts"
    assert popen.call_args.kwargs["stdin"] is stdin
    assert popen.call_args.kwargs["stderr"] is log_file
    written = "".join(c.args[0] for c in log_file.write.call_args_list)
    assert written.startswith("ffmpeg: ")


# ----------------------------------------------------------------------
# start(): wiring of log file, pipe, and outcome
# ----------------------------------------------------------------------


@pytest.fixture
def start_env(segment_worker, mocker):
    """start() with the filesystem, the run log and both children mocked out."""
    mocker.patch(f"{MODULE}.os.path.exists", return_value=False)
    mocker.patch(f"{MODULE}.os.makedirs")
    mocker.patch(f"{MODULE}.shutil.rmtree")
    log_file = MagicMock()
    mocker.patch(f"{MODULE}.StreamHelper.open_process_log", return_value=log_file)

    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = 0
    ffmpeg_proc.poll.return_value = 0
    create_ytdlp = mocker.patch.object(segment_worker, "_create_ytdlp_process", return_value=ytdlp_proc)
    create_ffmpeg = mocker.patch.object(segment_worker, "_create_ffmpeg_process", return_value=ffmpeg_proc)
    monitor = mocker.patch.object(segment_worker, "_monitor_segments")
    outcome = mocker.patch.object(segment_worker, "_log_outcome")
    return {
        "log_file": log_file,
        "ytdlp": ytdlp_proc,
        "ffmpeg": ffmpeg_proc,
        "create_ytdlp": create_ytdlp,
        "create_ffmpeg": create_ffmpeg,
        "monitor": monitor,
        "outcome": outcome,
    }


def test_start_wires_log_pipe_and_outcome(segment_worker, start_env):
    info = _info()

    segment_worker.start(info)

    env = start_env
    # Both children get the same run log; the parent's handle is closed after spawning.
    env["create_ytdlp"].assert_called_once_with(info, env["log_file"])
    env["create_ffmpeg"].assert_called_once()
    assert env["create_ffmpeg"].call_args.args[2] is env["ytdlp"].stdout
    assert env["create_ffmpeg"].call_args.args[3] is env["log_file"]
    env["log_file"].close.assert_called_once()
    # Our copy of the pipe's read end is dropped so a dead ffmpeg surfaces as EPIPE in yt-dlp.
    env["ytdlp"].stdout.close.assert_called_once()
    env["monitor"].assert_called_once()
    # Exit codes are sampled and reported with the path of the run log.
    env["outcome"].assert_called_once()
    assert env["outcome"].call_args.args[1:3] == (0, 0)
    assert env["outcome"].call_args.args[3].endswith("/tmp/key/live_segment.log")


def test_start_reports_outcome_even_if_monitor_raises(segment_worker, start_env):
    start_env["monitor"].side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        segment_worker.start(_info())

    start_env["outcome"].assert_called_once()


def test_start_stops_ytdlp_when_ffmpeg_fails_to_start(segment_worker, start_env):
    start_env["create_ffmpeg"].return_value = None
    start_env["ytdlp"].poll.return_value = None

    segment_worker.start(_info())

    start_env["ytdlp"].send_signal.assert_called_once_with(LiveSegmentWorker._YTDLP_STOP_SIGNAL)
    start_env["monitor"].assert_not_called()
    start_env["log_file"].close.assert_called_once()


def test_start_returns_when_ytdlp_fails_to_start(segment_worker, start_env):
    start_env["create_ytdlp"].return_value = None

    segment_worker.start(_info())

    start_env["create_ffmpeg"].assert_not_called()
    start_env["monitor"].assert_not_called()
    start_env["log_file"].close.assert_called_once()


def test_start_runs_without_log_file(segment_worker, start_env, mocker):
    mocker.patch(f"{MODULE}.StreamHelper.open_process_log", side_effect=OSError("read-only"))

    segment_worker.start(_info())

    start_env["create_ytdlp"].assert_called_once()
    assert start_env["create_ytdlp"].call_args.args[1] is None
    start_env["monitor"].assert_called_once()


# ----------------------------------------------------------------------
# Monitor: process death and stalls
# ----------------------------------------------------------------------


def _dying_ytdlp():
    """A yt-dlp mock that stays alive until it is signalled."""
    proc = MagicMock()
    proc.pid = 1234
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="yt-dlp", timeout=5)

    def stop(sig):
        proc.poll.return_value = -sig
        proc.wait.side_effect = None

    proc.send_signal.side_effect = stop
    return proc


def test_monitor_stops_ytdlp_when_ffmpeg_dies(segment_worker, mocker, caplog):
    """ffmpeg exited but yt-dlp lives on: it has nowhere to write, so it is stopped
    instead of the monitor waiting on both forever."""
    mocker.patch(f"{MODULE}.os.path.exists", return_value=False)
    mocker.patch(f"{MODULE}.time.sleep")
    segment_worker.stop_event.is_set.return_value = False
    ytdlp_proc = _dying_ytdlp()
    ffmpeg_proc = MagicMock()
    ffmpeg_proc.poll.return_value = 1
    ffmpeg_proc.returncode = 1

    with caplog.at_level(logging.WARNING):
        segment_worker._monitor_segments(_info(), "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    # yt-dlp is asked to stop the graceful way (it forwards "q" to its own ffmpeg).
    ytdlp_proc.send_signal.assert_called_once_with(LiveSegmentWorker._YTDLP_STOP_SIGNAL)
    assert "ffmpeg exited (code=1) while yt-dlp was still running" in caplog.text


def test_monitor_does_not_terminate_ytdlp_that_exits_on_its_own(segment_worker, mocker, caplog):
    """The normal end of a stream: yt-dlp closes the pipe, ffmpeg exits first and
    yt-dlp follows within the grace period. No warning, no terminate."""
    mocker.patch(f"{MODULE}.os.path.exists", return_value=False)
    mocker.patch(f"{MODULE}.time.sleep")
    segment_worker.stop_event.is_set.return_value = False
    ytdlp_proc = MagicMock()
    ytdlp_proc.poll.return_value = None

    def wait(timeout=None):
        ytdlp_proc.poll.return_value = 0

    ytdlp_proc.wait.side_effect = wait
    ffmpeg_proc = MagicMock()
    ffmpeg_proc.poll.return_value = 0

    with caplog.at_level(logging.WARNING):
        segment_worker._monitor_segments(_info(), "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    ytdlp_proc.send_signal.assert_not_called()
    ytdlp_proc.kill.assert_not_called()
    assert "while yt-dlp was still running" not in caplog.text


def test_monitor_stall_watchdog_terminates_ytdlp(segment_worker, mocker, caplog):
    """No segment for stale_ytdlp_seconds while yt-dlp is alive: yt-dlp is wedged
    (or the stream is gone without it noticing) and gets terminated so the run
    can end and the watcher can re-check the stream."""
    mocker.patch(f"{MODULE}.os.path.exists", return_value=False)
    clock = {"now": 1000.0}
    mocker.patch(f"{MODULE}.time.time", side_effect=lambda: clock["now"])
    # Each idle wait moves the fake clock forward by 100s.
    mocker.patch(f"{MODULE}.time.sleep", side_effect=lambda _: clock.__setitem__("now", clock["now"] + 100.0))
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.stale_ytdlp_seconds = 180
    ytdlp_proc = _dying_ytdlp()
    ffmpeg_proc = MagicMock()
    ffmpeg_proc.poll.return_value = None

    # yt-dlp exits on the first signal; ffmpeg sees EOF once yt-dlp is gone and exits by itself.
    def stop(sig):
        ytdlp_proc.poll.return_value = -sig
        ytdlp_proc.wait.side_effect = None
        ffmpeg_proc.poll.return_value = 0

    ytdlp_proc.send_signal.side_effect = stop

    with caplog.at_level(logging.WARNING):
        segment_worker._monitor_segments(_info(), "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    ytdlp_proc.send_signal.assert_called_once_with(LiveSegmentWorker._YTDLP_STOP_SIGNAL)
    assert "No new segment in 200s" in caplog.text


def _run_unusable_monitor(worker, mocker, durations):
    """Drive _monitor_segments over always-ready segments with the given
    durations (0 = no usable data), advancing a fake clock 6s per segment."""
    clock = {"now": 1000.0}
    mocker.patch(f"{MODULE}.time.time", side_effect=lambda: clock["now"])
    mocker.patch(f"{MODULE}.os.path.exists", return_value=True)
    mocker.patch(f"{MODULE}.os.remove")
    mocker.patch(f"{MODULE}.open", mock_open(read_data=b"segment-data"), create=True)
    mocker.patch(f"{MODULE}.os.fstat", return_value=MagicMock(st_mtime=1000.0))
    mocker.patch(f"{MODULE}.StreamHelper.describe_codecs", return_value="video=h264, audio=aac")

    def duration(_data):
        clock["now"] += 6.0
        return durations.pop(0)

    mocker.patch(f"{MODULE}.StreamHelper.get_precise_duration", side_effect=duration)
    worker.stop_event.is_set.side_effect = lambda: not durations
    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = None
    ffmpeg_proc.poll.return_value = None

    worker._monitor_segments(_info(), "/tmp/segdir", ytdlp_proc, ffmpeg_proc)
    return durations


def test_monitor_unusable_watchdog_ends_run(segment_worker, mocker, caplog):
    """Segments keep arriving (so the stall watchdog never fires) but none is
    usable: after stale_unusable_seconds the run ends and is flagged for restart."""
    segment_worker.stale_unusable_seconds = 60

    with caplog.at_level(logging.WARNING):
        remaining = _run_unusable_monitor(segment_worker, mocker, [0.0] * 20)

    # 6s per segment: the 11th unusable segment is the first past 60s.
    assert len(remaining) == 9
    assert segment_worker._unusable_stall is True
    assert "No usable segment in 66s; ending this run." in caplog.text


def test_monitor_unusable_watchdog_reset_by_usable_segment(segment_worker, mocker):
    """An occasional unusable segment between good ones never trips the watchdog."""
    segment_worker.stale_unusable_seconds = 60

    remaining = _run_unusable_monitor(segment_worker, mocker, ([0.0] * 9 + [6.0]) * 3)

    assert remaining == []
    assert segment_worker._unusable_stall is False
    assert segment_worker.queue.put.call_count == 3


def test_start_restarts_pipeline_after_unusable_stall(segment_worker, start_env):
    """LiveSegmentWorker relaunches yt-dlp/ffmpeg after an unusable-data stall."""
    stalls = iter([True, False])

    def monitor(*_args):
        segment_worker._unusable_stall = next(stalls)

    start_env["monitor"].side_effect = monitor
    segment_worker.stop_event.is_set.return_value = False

    segment_worker.start(_info())

    assert start_env["create_ytdlp"].call_count == 2
    assert start_env["create_ffmpeg"].call_count == 2
    assert start_env["outcome"].call_count == 2


def test_start_does_not_restart_when_stopping(segment_worker, start_env):
    def monitor(*_args):
        segment_worker._unusable_stall = True

    start_env["monitor"].side_effect = monitor
    segment_worker.stop_event.is_set.return_value = True

    segment_worker.start(_info())

    start_env["create_ytdlp"].assert_called_once()


def test_start_twitch_lfs_does_not_restart_after_unusable_stall(start_env, mocker):
    """A --live-from-start relaunch would replay the stream; the LFS run just ends."""
    from live_transcript_worker.worker_twitch_lfs import TwitchLFSWorker

    lfs = TwitchLFSWorker("key", MagicMock(), MagicMock())
    lfs.stop_event.is_set.return_value = False
    create_ytdlp = mocker.patch.object(lfs, "_create_ytdlp_process", return_value=start_env["ytdlp"])
    mocker.patch.object(lfs, "_create_ffmpeg_process", return_value=start_env["ffmpeg"])
    mocker.patch.object(lfs, "_log_outcome")

    def monitor(*_args):
        lfs._unusable_stall = True

    mocker.patch.object(lfs, "_monitor_segments", side_effect=monitor)

    lfs.start(_info(url="https://www.twitch.tv/abc"))

    create_ytdlp.assert_called_once()


def test_log_outcome_warns_after_unusable_stall(segment_worker, mocker, caplog):
    mocker.patch.object(segment_worker, "_append_log")
    mocker.patch(f"{MODULE}.StreamHelper.trim_log_file")
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.segments_produced = 5
    segment_worker._unusable_stall = True

    with caplog.at_level(logging.WARNING):
        segment_worker._log_outcome(_info(), None, None, "/tmp/key/live_segment.log")

    assert "Stopped after only unusable segments" in caplog.text


def test_monitor_stops_ffmpeg_that_never_drains(segment_worker, mocker, caplog):
    """yt-dlp is gone but ffmpeg never exits: stop it after the drain timeout
    rather than spinning forever."""
    mocker.patch(f"{MODULE}.os.path.exists", return_value=False)
    clock = {"now": 1000.0}
    mocker.patch(f"{MODULE}.time.time", side_effect=lambda: clock["now"])
    mocker.patch(f"{MODULE}.time.sleep", side_effect=lambda _: clock.__setitem__("now", clock["now"] + 20.0))
    segment_worker.stop_event.is_set.return_value = False
    ytdlp_proc = MagicMock()
    ytdlp_proc.poll.return_value = 0
    ffmpeg_proc = MagicMock()
    ffmpeg_proc.poll.return_value = None
    ffmpeg_proc.send_signal.side_effect = lambda sig: ffmpeg_proc.poll.configure_mock(return_value=-sig)

    with caplog.at_level(logging.WARNING):
        segment_worker._monitor_segments(_info(), "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    # ffmpeg itself is stopped with a plain SIGTERM (which it handles gracefully).
    ffmpeg_proc.send_signal.assert_called_once_with(signal.SIGTERM)
    assert "ffmpeg still running 30s after yt-dlp exited" in caplog.text


def test_stop_process_escalates_to_kill(segment_worker, mocker):
    killpg = mocker.patch(f"{MODULE}.os.killpg")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=5), None]

    segment_worker._stop_process(proc)

    proc.send_signal.assert_called_once_with(signal.SIGTERM)
    proc.kill.assert_called_once()
    # ffmpeg shares our process group: never killpg it.
    killpg.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="SIGINT and process groups are POSIX-only")
def test_stop_ytdlp_retries_sigint_then_kills_process_group(segment_worker, mocker):
    """A download stalled in a socket read never acts on yt-dlp's "q", so the first
    SIGINT hangs in yt-dlp's interrupt handler. The second SIGINT makes yt-dlp
    kill its ffmpeg and exit; if even that fails, the whole session is SIGKILLed
    so the inner ffmpeg cannot be left holding the segmenter's pipe open."""
    killpg = mocker.patch(f"{MODULE}.os.killpg")
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    timeout = subprocess.TimeoutExpired(cmd="yt-dlp", timeout=5)
    proc.wait.side_effect = [timeout, timeout, None]

    segment_worker._stop_ytdlp(proc)

    assert proc.send_signal.call_args_list == [call(signal.SIGINT), call(signal.SIGINT)]
    killpg.assert_called_once_with(4242, signal.SIGKILL)
    proc.kill.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="SIGINT is POSIX-only")
def test_stop_ytdlp_second_sigint_is_enough(segment_worker, mocker):
    killpg = mocker.patch(f"{MODULE}.os.killpg")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="yt-dlp", timeout=5), None]

    segment_worker._stop_ytdlp(proc)

    assert proc.send_signal.call_args_list == [call(signal.SIGINT), call(signal.SIGINT)]
    killpg.assert_not_called()
    proc.kill.assert_not_called()


def test_stop_process_noop_when_already_exited(segment_worker):
    proc = MagicMock()
    proc.poll.return_value = 1

    segment_worker._stop_process(proc)

    proc.send_signal.assert_not_called()
    proc.kill.assert_not_called()


# ----------------------------------------------------------------------
# Outcome reporting
# ----------------------------------------------------------------------


def test_log_outcome_warns_and_points_at_log_when_no_segments(segment_worker, mocker, caplog):
    mocker.patch.object(segment_worker, "_append_log")
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.segments_produced = 0

    with caplog.at_level(logging.WARNING):
        segment_worker._log_outcome(_info(), 1, 1, "/app/tmp/key/live_segment.log")

    assert "produced no segments" in caplog.text
    assert "yt-dlp exit=1, ffmpeg exit=1, segments=0" in caplog.text
    assert "/app/tmp/key/live_segment.log" in caplog.text


def test_log_outcome_warns_on_error_exit_after_segments(segment_worker, mocker, caplog):
    mocker.patch.object(segment_worker, "_append_log")
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.segments_produced = 40

    with caplog.at_level(logging.WARNING):
        segment_worker._log_outcome(_info(), 1, 0, "/app/tmp/key/live_segment.log")

    assert "yt-dlp exited with an error" in caplog.text
    assert "segments=40" in caplog.text


def test_log_outcome_silent_on_healthy_run(segment_worker, mocker, caplog):
    mocker.patch.object(segment_worker, "_append_log")
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.segments_produced = 40

    with caplog.at_level(logging.INFO):
        segment_worker._log_outcome(_info(), 0, 0, "/app/tmp/key/live_segment.log")

    assert "Run finished" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_log_outcome_silent_during_shutdown(segment_worker, mocker, caplog):
    mocker.patch.object(segment_worker, "_append_log")
    # Stopped by us: still running when sampled, negative exit code afterwards.
    segment_worker.stop_event.is_set.return_value = True
    segment_worker.segments_produced = 0

    with caplog.at_level(logging.INFO):
        segment_worker._log_outcome(_info(), None, None, "/app/tmp/key/live_segment.log")

    assert "Stopped on request" in caplog.text
    assert "yt-dlp exit=running" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_log_outcome_writes_footer_to_run_log(segment_worker, tmp_path):
    log_path = tmp_path / "live_segment.log"
    log_path.write_text("--- LiveSegmentWorker started ---\nERROR: something\n")
    segment_worker.stop_event.is_set.return_value = False
    segment_worker.segments_produced = 3

    segment_worker._log_outcome(_info(), 0, 0, str(log_path))

    lines = log_path.read_text().splitlines()
    assert lines[-1].startswith("--- LiveSegmentWorker stopped at ")
    assert "yt-dlp exit=0, ffmpeg exit=0, segments=3" in lines[-1]
