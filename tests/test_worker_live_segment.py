from unittest.mock import MagicMock, mock_open

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_live_segment import LiveSegmentWorker


@pytest.fixture
def segment_worker(mocker):
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    mocker.patch("live_transcript_worker.helper.Config")
    queue = MagicMock()
    stop_event = MagicMock()
    worker = LiveSegmentWorker("key", queue, stop_event)
    worker.buffer_size_seconds = 6
    worker.live_latency_seconds = 1
    return worker


def _run_monitor(worker, mocker, mtimes, duration=6.0):
    """Drive _monitor_segments over len(mtimes) always-ready segments, each
    reporting the given file mtime and duration, then return the
    audio_start_time queued for each segment."""
    info = StreamInfoObject(url="url", key="key", media_type=Media.AUDIO)

    # Every segment is "ready"; both processes stay alive so the loop never
    # short-circuits on the both_done checks.
    mocker.patch("live_transcript_worker.worker_live_segment.os.path.exists", return_value=True)
    mocker.patch("live_transcript_worker.worker_live_segment.os.remove")
    # `open` is patched only within the worker module's namespace (create=True),
    # so we don't disturb file access elsewhere during the test.
    mocker.patch("live_transcript_worker.worker_live_segment.open", mock_open(read_data=b"segment-data"), create=True)
    # Each segment's file mtime is what anchors its timestamp. os.fstat is called
    # once per segment, from the open handle.
    mocker.patch(
        "live_transcript_worker.worker_live_segment.os.fstat",
        side_effect=[MagicMock(st_mtime=m) for m in mtimes],
    )
    mocker.patch(
        "live_transcript_worker.worker_live_segment.StreamHelper.get_precise_duration",
        return_value=duration,
    )
    mocker.patch("live_transcript_worker.worker_live_segment.time.sleep")
    worker.stop_event.is_set.side_effect = [False] * len(mtimes) + [True]

    ytdlp_proc = MagicMock()
    ffmpeg_proc = MagicMock()
    ytdlp_proc.poll.return_value = None
    ffmpeg_proc.poll.return_value = None

    worker._monitor_segments(info, "/tmp/segdir", ytdlp_proc, ffmpeg_proc)

    return [call.args[0].audio_start_time for call in worker.queue.put.call_args_list]


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
