import json
import logging
from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_youtube_timeline import YouTubeTimelineWorker
from live_transcript_worker.youtube_timeline import (
    CONTAINER_ADTS,
    CONTAINER_MPEGTS,
    TRACK_AUDIO,
    TRACK_VIDEO,
    TS_WRAP_SECONDS,
    SegmentGroup,
    TrackSegment,
)
from live_transcript_worker.youtube_timeline_source import (
    ResolvedStream,
    ResolvedTrack,
    ResolveError,
    ResolveStopped,
    SegmentSource,
    SourceError,
    SourceExpiredError,
    SourceResetError,
    StreamNotLiveError,
    StreamUnavailableError,
    UnsupportedSourceError,
)

W = "live_transcript_worker.worker_youtube_timeline"
START = 1_791_932_469.0
SEG = 1024 * 43 / 44100  # one 1 s HLS audio segment


class Clock:
    """Monotonic and wall clock moving together."""

    def __init__(self, wall_at_zero: float):
        self.t = 0.0
        self.wall_at_zero = wall_at_zero

    def mono(self):
        return self.t

    def wall(self):
        return self.wall_at_zero + self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


def group(sequence, clock, video=True, audio=True):
    segments = {}
    if audio:
        segments[TRACK_AUDIO] = TrackSegment(TRACK_AUDIO, sequence, b"a", CONTAINER_ADTS, clock, SEG, 512 / 44100)
    if video:
        segments[TRACK_VIDEO] = TrackSegment(TRACK_VIDEO, sequence, b"v", CONTAINER_MPEGTS, clock + 0.01, 1.0)
    return SegmentGroup(sequence, segments)


def run_of(first, count, clock0):
    """`count` contiguous groups starting at sequence `first` and clock `clock0`."""
    return [group(first + i, clock0 + i * SEG) for i in range(count)]


def resolved(expires_at=None, tracks=(TRACK_VIDEO, TRACK_AUDIO)):
    return ResolvedStream("abcdefghijk", "is_live", tuple(ResolvedTrack(t, "m3u8_native", f"https://m/{t}", t) for t in tracks), expires_at)


class FakeSource(SegmentSource):
    """Plays a script: a SegmentGroup, None (nothing new, takes `timeout`),
    ("sleep", s) (time passes), "end" (ENDLIST), or an exception to raise."""

    wrap_period = TS_WRAP_SECONDS
    vod_offset = 0.036

    def __init__(self, resolved_stream, resume, ctx, script, clock, step=1.0):
        super().__init__(resolved_stream, resume, ctx)
        self.script = list(script)
        self.clock = clock
        self.step = step
        self.resume = resume
        self.closed = False

    def next_group(self, timeout):
        if not self.script:
            self.clock.sleep(timeout)
            return None
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        if action == "end":
            self._ended = True
            return None
        if isinstance(action, tuple):
            self.clock.sleep(action[1])
            return None
        if action is None:
            self.clock.sleep(timeout)
            return None
        self.clock.sleep(self.step)
        self._next_sequence = action.sequence + 1
        return action

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, mocker, tmp_path, start_time=str(START), stream_id="abcdefghijk", media_type=Media.VIDEO):
        mocker.patch("live_transcript_worker.worker_abstract.Config")
        self.queue = MagicMock()
        self.stop = MagicMock()
        self.stop.is_set.return_value = False
        self.worker = YouTubeTimelineWorker("key", self.queue, self.stop)
        w = self.worker
        w.buffer_size_seconds = 6
        w.stale_unusable_seconds = 60
        w.stale_lfs_gap_seconds = 600
        w.live_latency_seconds = 1
        w._TMP_ROOT = str(tmp_path)  # type: ignore[misc]
        # Wall time a few seconds after the clock values used by default (stream ~5000 s in).
        self.clock = Clock(START + 5000.0 - 0.0)
        w._monotonic = self.clock.mono  # type: ignore[method-assign]
        w._wallclock = self.clock.wall  # type: ignore[method-assign]
        w._sleep = self.clock.sleep  # type: ignore[method-assign]
        self.info = StreamInfoObject(
            url="https://www.youtube.com/@chan/live", key="key", stream_id=stream_id, start_time=start_time, media_type=media_type
        )
        self.resolutions: list = []
        self.resolve_calls: list[float] = []
        self.scripts: list = []
        self.sources: list[FakeSource] = []
        self.steps: list[float] = []
        mocker.patch(f"{W}.resolve_stream", side_effect=self._resolve)
        mocker.patch(f"{W}.create_segment_source", side_effect=self._create)
        mocker.patch(f"{W}.mux_chunk", side_effect=lambda chunk, workdir: b"TS:%d-%d" % (chunk.first_sequence, chunk.last_sequence))
        mocker.patch(f"{W}.StreamHelper.get_precise_duration", return_value=6.0)
        mocker.patch(f"{W}.StreamHelper.describe_codecs", return_value="video=h264, audio=aac")

    def _resolve(self, ytdlp_path, url, media_type, stop_event, log_file=None):
        self.resolve_calls.append(self.clock.t)
        assert url == "https://www.youtube.com/watch?v=abcdefghijk"
        if not self.resolutions:
            raise StreamNotLiveError("live_status is was_live")
        item = self.resolutions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _create(self, resolved_stream, resume, ctx):
        script = self.scripts.pop(0) if self.scripts else ["end"]
        step = self.steps.pop(0) if self.steps else 1.0
        source = FakeSource(resolved_stream, resume, ctx, script, self.clock, step)
        self.sources.append(source)
        return source

    def run(self):
        self.worker.start(self.info)
        return [c.args[0] for c in self.queue.put.call_args_list]


@pytest.fixture
def h(mocker, tmp_path):
    return Harness(mocker, tmp_path)


# ----------------------------------------------------------------------
# Timestamps
# ----------------------------------------------------------------------


def test_lines_are_stamped_from_the_stream_clock(h):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 13, 4990.0) + ["end"]]
    items = h.run()
    # 6 + 6 sequences, then the 1-sequence leftover flushed at the end.
    assert [i.raw for i in items] == [b"TS:100-105", b"TS:106-111", b"TS:112-112"]
    for item, first_clock in zip(items, (4990.0, 4990.0 + 6 * SEG, 4990.0 + 12 * SEG), strict=True):
        assert item.vod_accurate is True
        assert item.audio_start_time == pytest.approx(START + first_clock + 0.036)
        assert item.media_type == Media.VIDEO and item.key == "key"


def test_resume_point_is_saved_after_each_chunk(h, tmp_path):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 7, 4990.0) + ["end"]]
    h.run()
    state = json.loads((tmp_path / "key" / "youtube_timeline_state.json").read_text())
    assert state == {"stream_id": "abcdefghijk", "next_sequence": 107, "anchor": [pytest.approx(4990.0 + 7 * SEG), h.clock.wall()]}


def test_a_new_run_of_the_same_stream_resumes_where_the_last_one_stopped(h, mocker, tmp_path):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    h.run()
    h2 = Harness(mocker, tmp_path)
    h2.resolutions = [resolved()]
    h2.run()
    assert h2.sources[0].resume == 106
    # A different stream on the same key doesn't pick up that resume point.
    h3 = Harness(mocker, tmp_path, stream_id="zzzzzzzzzzz")
    mocker.patch(f"{W}.watch_url", return_value="https://www.youtube.com/watch?v=abcdefghijk")
    h3.resolutions = [resolved()]
    h3.run()
    assert len(h3.sources) == 1 and h3.sources[0].resume is None


def test_unknown_start_time_is_estimated(mocker, tmp_path):
    h = Harness(mocker, tmp_path, start_time="0")
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    items = h.run()
    assert len(items) == 1 and items[0].vod_accurate is True
    # Estimated from arrival: the line lands near the wall clock.
    assert abs(items[0].audio_start_time - (h.clock.wall() - 6)) < 10


def test_wrap_count_is_seeded_from_the_stream_start(mocker, tmp_path):
    """27 h into a stream the 33-bit clock has wrapped once; a fresh run must not
    stamp lines 26.5 h too early."""
    h = Harness(mocker, tmp_path, start_time=str(START))
    h.clock.wall_at_zero = START + 27 * 3600 + 3
    raw = 27 * 3600 - TS_WRAP_SECONDS
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, raw) + ["end"]]
    items = h.run()
    assert items[0].audio_start_time == pytest.approx(START + 27 * 3600 + 0.036)


def test_a_clock_that_jumps_back_is_not_trusted(h, caplog):
    h.resolutions = [resolved()]
    # The sequence continues but the clock restarts near zero: a timeline reset.
    h.scripts = [run_of(100, 6, 4990.0) + run_of(106, 6, 10.0) + ["end"]]
    caplog.set_level(logging.WARNING)
    items = h.run()
    assert [i.vod_accurate for i in items] == [True, False]
    assert items[1].audio_start_time == pytest.approx(h.clock.wall() - 6.0 - 1, abs=10)
    assert "not marked VOD-accurate" in caplog.text


def test_a_clock_that_restarted_long_ago_is_not_trusted(mocker, tmp_path, caplog):
    """Seen live on a 24/7 stream: the sequence numbers and the clock restarted
    days after the stream started, so start time + clock lands hours away from
    now. Those lines can't be VOD-accurate."""
    h = Harness(mocker, tmp_path)
    h.clock.wall_at_zero = START + 6_534_178.0  # 75 days in
    raw = 59_700.0  # the clock counts from a restart ~4 days ago
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, raw) + ["end"]]
    caplog.set_level(logging.WARNING)
    items = h.run()
    assert [i.vod_accurate for i in items] == [False]
    assert items[0].audio_start_time == pytest.approx(h.clock.wall() - 6.0 - 1, abs=10)
    assert "doesn't count from the stream's start" in caplog.text


def test_an_outage_longer_than_the_start_time_check_allows_stays_vod_accurate(h):
    """Seen live: YouTube's clock, like the VOD, skips the time a stream is down,
    so after outages start + clock falls behind now by their total length. Lines
    after them are still exactly where the VOD has them."""
    outage = 2000.0  # well past lfs_gap_seconds + 300 s
    h.resolutions = [resolved(), resolved()]
    h.scripts = [run_of(100, 7, 4990.0) + [("sleep", outage)], run_of(107, 12, 4990.0 + 7 * SEG) + ["end"]]
    items = h.run()
    assert [i.raw for i in items] == [b"TS:100-105", b"TS:106-111", b"TS:112-117", b"TS:118-118"]
    assert [i.vod_accurate for i in items] == [True] * 4
    assert items[1].audio_start_time == pytest.approx(START + 4990.0 + 6 * SEG + 0.036)
    assert h.worker.reconnects == 1


def test_segments_lost_in_an_outage_stay_vod_accurate(h):
    h.resolutions = [resolved(), resolved()]
    h.scripts = [run_of(100, 7, 4990.0) + [("sleep", 2000.0)], run_of(400, 7, 4990.0 + 300 * SEG) + ["end"]]
    items = h.run()
    assert all(i.vod_accurate for i in items)
    after = next(i for i in items if i.raw.startswith(b"TS:400-"))
    assert after.audio_start_time == pytest.approx(START + 4990.0 + 300 * SEG + 0.036)


def test_a_clock_that_leaps_ahead_is_not_trusted(h):
    """Continuing a timeline means never running ahead of the time that passed."""
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 7, 4990.0) + run_of(107, 6, 90_000.0) + ["end"]]
    items = h.run()
    assert [i.vod_accurate for i in items] == [True, True, False]


def test_a_restarted_run_continues_the_last_runs_timeline(h, mocker, tmp_path):
    """The worker restarted mid-stream, after outages the stream clock skipped:
    its first chunk carries on exactly where the last run's ended."""
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    assert [i.vod_accurate for i in h.run()] == [True]
    h2 = Harness(mocker, tmp_path)
    h2.clock.wall_at_zero = h.clock.wall() + 2000.0
    h2.resolutions = [resolved()]
    h2.scripts = [run_of(106, 6, 4990.0 + 6 * SEG) + ["end"]]
    items = h2.run()
    assert [i.vod_accurate for i in items] == [True]
    assert items[0].audio_start_time == pytest.approx(START + 4990.0 + 6 * SEG + 0.036)
    # Not an exact continuation: checked against the start time as usual.
    h3 = Harness(mocker, tmp_path)
    h3.clock.wall_at_zero = h2.clock.wall() + 2000.0
    h3.resolutions = [resolved()]
    h3.scripts = [run_of(150, 6, 4990.0 + 50 * SEG) + ["end"]]
    assert [i.vod_accurate for i in h3.run()] == [False]


def test_catching_up_a_backlog_stays_vod_accurate(h):
    """Ten minutes behind (after a restart) is still the stream's own clock."""
    h.clock.wall_at_zero = START + 5000.0 + 550.0
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    items = h.run()
    assert [i.vod_accurate for i in items] == [True]


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_not_live_ends_without_a_connection(h):
    h.resolutions = [StreamNotLiveError("live_status is was_live")]
    assert h.run() == []
    assert h.sources == []


def test_members_only_ends(h):
    h.resolutions = [StreamUnavailableError("members-only")]
    assert h.run() == []


def test_resolve_failures_back_off(h):
    h.resolutions = [ResolveError("timed out"), ResolveError("timed out"), resolved()]
    h.scripts = [["end"]]
    h.run()
    gaps = [b - a for a, b in zip(h.resolve_calls, h.resolve_calls[1:], strict=False)]
    assert gaps[0] >= 15 and gaps[1] >= 30


def test_resolve_backoff_stays_under_half_the_catch_up_window(h):
    """With lfs_gap_seconds at 300, waiting 300 s between failed yt-dlp runs could
    leave a stream that came back right after one more than 300 s behind by the
    next, and the source would skip everything since it came back."""
    h.worker.stale_lfs_gap_seconds = 300
    h.resolutions = [ResolveError("got no usable formats")] * 6 + [resolved()]
    h.scripts = [["end"]]
    h.run()
    gaps = [b - a for a, b in zip(h.resolve_calls, h.resolve_calls[1:], strict=False)]
    assert gaps == pytest.approx([15, 30, 60, 120, 150, 150], abs=1)


def test_unsupported_is_logged_once_and_retried_rarely(h, caplog):
    caplog.set_level(logging.ERROR)
    h.resolutions = [UnsupportedSourceError("SABR only"), UnsupportedSourceError("SABR only"), StreamNotLiveError("ended")]
    h.run()
    assert h.resolve_calls[1] - h.resolve_calls[0] >= 600
    assert caplog.text.count("cannot read this stream") == 1


def test_unsupported_from_the_source_factory_is_retried_rarely(h, mocker):
    calls = []

    def create(resolved_stream, resume, ctx):
        calls.append(h.clock.t)
        raise UnsupportedSourceError("no segment source for protocols ['sabr']")

    mocker.patch(f"{W}.create_segment_source", side_effect=create)
    h.resolutions = [resolved(), resolved()]
    h.run()
    assert len(calls) == 2 and calls[1] - calls[0] >= 600


def test_resolve_stopped_ends(h):
    h.resolutions = [ResolveStopped("stopped while resolving")]
    assert h.run() == []


def test_expired_urls_reconnect_and_continue_at_the_same_sequence(h):
    h.resolutions = [resolved(), resolved()]
    h.scripts = [run_of(100, 4, 4990.0) + [SourceExpiredError("audio sequence 104: HTTP 403")], run_of(104, 2, 4990.0 + 4 * SEG) + ["end"]]
    items = h.run()
    assert h.sources[1].resume == 104
    assert h.sources[0].closed
    # The chunk spans the reconnect: nothing lost, no gap.
    assert [i.raw for i in items] == [b"TS:100-105"]
    assert h.worker.reconnects == 1


def test_idle_connection_reconnects_then_ends_when_the_stream_is_over(h):
    h.resolutions = [resolved()]  # the second resolve says not live
    h.scripts = [run_of(100, 2, 4990.0) + [None] * 30]
    h.run()
    assert len(h.resolve_calls) == 2
    assert h.worker.reconnects == 1


def test_frozen_playlist_does_not_end_the_run_while_the_stream_is_live(h):
    """A streamer outage: no segments for minutes, but yt-dlp still says live.
    The run keeps going (paced) instead of returning to the watcher."""
    h.resolutions = [resolved(), resolved(), resolved(), resolved()]
    h.scripts = [[None] * 20, [None] * 20, [None] * 20, run_of(100, 6, 4990.0) + ["end"]]
    items = h.run()
    assert len(items) == 1
    assert len(h.resolve_calls) == 4
    # Fruitless reconnects back off.
    gaps = [b - a for a, b in zip(h.resolve_calls, h.resolve_calls[1:], strict=False)]
    assert gaps[1] > gaps[0]


def test_nothing_usable_reconnects(h, caplog):
    """Segments keep coming (so the idle check never fires) but none has audio."""
    caplog.set_level(logging.INFO)
    h.resolutions = [resolved()]
    h.scripts = [[group(100 + i, 4990.0 + i * SEG, audio=False) for i in range(70)]]
    h.run()
    assert h.worker.reconnects == 1
    assert len(h.resolve_calls) == 2
    assert "reconnecting: nothing usable in 61s" in caplog.text
    # The connection produced nothing, so the next yt-dlp run backs off 15 s.
    assert h.resolve_calls[1] == pytest.approx(61 + 15, abs=1)


def test_repeated_source_errors_reconnect(h):
    h.resolutions = [resolved()]
    h.scripts = [[SourceError("audio playlist: Timeout")] * 3]
    h.run()
    assert h.worker.reconnects == 1


def _clock_of(sequence):
    return 4990.0 + (sequence - 100) * SEG


def test_proactive_refresh_swaps_urls_without_a_gap(h, mocker):
    expires = h.clock.wall() + 20 * 60 + 2.5
    h.resolutions = [resolved(expires_at=expires), resolved(expires_at=expires + 6 * 3600)]

    def create(resolved_stream, resume, ctx):
        if not h.sources:
            # A healthy connection: a new segment every second, indefinitely.
            script = (group(s, _clock_of(s)) for s in range(100, 10_000))
        else:
            script = iter([group(resume, _clock_of(resume)), group(resume + 1, _clock_of(resume + 1)), "end"])
        source = FakeSource(resolved_stream, resume, ctx, [], h.clock)
        source.script = script  # type: ignore[assignment]
        source.next_group = lambda timeout, src=source: _play(src, timeout)  # type: ignore[method-assign]
        h.sources.append(source)
        return source

    mocker.patch(f"{W}.create_segment_source", side_effect=create)
    items = h.run()
    assert len(h.resolve_calls) == 2
    swap_at = h.sources[1].resume
    assert swap_at == h.sources[0].checkpoint and swap_at > 100
    assert h.sources[0].closed
    assert h.worker.reconnects == 0  # a swap, not a reconnect
    # Every sequence from 100 to the end was queued exactly once, across the swap.
    covered = []
    for item in items:
        first, last = (int(x) for x in item.raw[3:].split(b"-"))
        covered += list(range(first, last + 1))
    assert covered == list(range(100, swap_at + 2))
    assert all(i.vod_accurate for i in items)


def _play(source, timeout):
    action = next(source.script, None)
    if action is None:
        source.clock.sleep(timeout)
        return None
    if action == "end":
        source._ended = True
        return None
    source.clock.sleep(1.0)
    source._next_sequence = action.sequence + 1
    return action


def test_failed_proactive_refresh_keeps_the_current_urls_until_hard_expiry(h):
    expires = h.clock.wall() + 20 * 60 - 1
    h.resolutions = [resolved(expires_at=expires), ResolveError("timed out"), StreamNotLiveError("ended")]
    # Keep the connection alive with segments; then let time run out.
    h.scripts = [run_of(100, 6, 4990.0) + [("sleep", 19 * 60)] + run_of(106, 3, 4990.0 + 6 * SEG)]
    items = h.run()
    assert [i.raw for i in items][0] == b"TS:100-105"
    # Refresh attempt failed, the connection carried on, then the hard expiry reconnected.
    assert len(h.resolve_calls) == 3
    assert h.worker.reconnects == 1


def test_an_expiry_too_close_to_trust_is_ignored(h, caplog):
    caplog.set_level(logging.WARNING)
    h.resolutions = [resolved(expires_at=h.clock.wall() + 60)]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    h.run()
    assert len(h.resolve_calls) == 1
    assert "ignoring that" in caplog.text


def test_reset_follows_the_new_numbering_on_the_same_urls(h):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 3, 4990.0) + [SourceResetError("sequence numbers went back")], run_of(5, 6, 4.0) + ["end"]]
    items = h.run()
    # Same URLs, no yt-dlp run, read from the start of the new numbering (the source clamps it).
    assert len(h.resolve_calls) == 1
    assert h.sources[1].resume == 0
    assert h.sources[1].resolved is h.sources[0].resolved
    # The partial chunk before the reset is flushed, then the new timeline starts fresh.
    assert [i.raw for i in items][:2] == [b"TS:100-102", b"TS:5-10"]


def test_missing_video_triggers_a_rate_limited_reconnect(h):
    h.resolutions = [resolved(), resolved()]

    class VideoGone(FakeSource):
        def next_group(self, timeout):
            g = super().next_group(timeout)
            if g is not None:
                self.consecutive_missing[TRACK_VIDEO] = 40
            return g

    created = []

    def create(resolved_stream, resume, ctx):
        script = run_of(100, 6, 4990.0) if not created else run_of(106, 6, 4990.0 + 6 * SEG) + ["end"]
        source = VideoGone(resolved_stream, resume, ctx, script, h.clock)
        created.append(source)
        return source

    import live_transcript_worker.worker_youtube_timeline as module

    module.create_segment_source = create  # type: ignore[assignment]
    h.run()
    # One reconnect for the missing video; the second connection isn't cut short again.
    assert len(created) == 2 and h.worker.reconnects == 1


def test_stop_flushes_the_partial_chunk_and_exits(h):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 3, 4990.0) + [None] * 100]
    calls = {"n": 0}

    def is_set():
        calls["n"] += 1
        return calls["n"] > 12

    h.stop.is_set.side_effect = is_set
    items = h.run()
    assert [i.raw for i in items] == [b"TS:100-102"]
    assert len(h.resolve_calls) == 1


def test_unexpected_errors_are_contained(h, caplog):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 2, 4990.0) + [RuntimeError("boom")]]
    caplog.set_level(logging.ERROR)
    items = h.run()  # must not raise
    assert [i.raw for i in items] == [b"TS:100-101"]
    assert "Unexpected error" in caplog.text


def test_mux_failures_are_not_queued(h, mocker):
    from live_transcript_worker.youtube_timeline import MuxError

    mocker.patch(f"{W}.mux_chunk", side_effect=MuxError("ffmpeg exit=1"))
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    assert h.run() == []
    assert h.worker.chunks_failed == 1


def test_run_log_records_the_outcome(h, tmp_path):
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + ["end"]]
    h.run()
    log = (tmp_path / "key" / "youtube_timeline.log").read_text()
    assert "YouTubeTimelineWorker started" in log
    assert "resolved video=video, audio=audio" in log
    assert "stopped at" in log and "the stream ended" in log and "chunks=1" in log
    assert not (tmp_path / "key" / "youtube_timeline").exists()  # work dir cleaned up


def test_a_slow_url_refresh_does_not_block_segments(h, mocker):
    """The refresh runs yt-dlp off the worker thread; segments keep being
    consumed meanwhile and nothing is torn down."""
    import threading

    expires = h.clock.wall() + 20 * 60 - 1
    release = threading.Event()
    in_refresh = threading.Event()
    calls = []

    def resolve(ytdlp_path, url, media_type, stop_event, log_file=None):
        calls.append(h.clock.t)
        if len(calls) == 1:
            return resolved(expires_at=expires)
        in_refresh.set()
        release.wait(10)
        return resolved(expires_at=expires + 6 * 3600)

    mocker.patch(f"{W}.resolve_stream", side_effect=resolve)
    consumed_during_refresh = []

    def create(resolved_stream, resume, ctx):
        source = FakeSource(resolved_stream, resume, ctx, [], h.clock)
        first = not h.sources
        h.sources.append(source)

        state = {"s": resume or 100}

        def play(timeout, src=source, state=state):
            if not first:
                if state["s"] > (resume or 100) + 1:
                    src._ended = True
                    return None
            elif in_refresh.is_set():
                consumed_during_refresh.append(state["s"])
                if len(consumed_during_refresh) >= 70:  # 70 s of segments while yt-dlp hangs
                    release.set()
            seq = state["s"]
            state["s"] += 1
            h.clock.sleep(1.0)
            src._next_sequence = seq + 1
            return group(seq, 4990.0 + (seq - 100) * SEG)

        source.next_group = play  # type: ignore[method-assign]
        return source

    mocker.patch(f"{W}.create_segment_source", side_effect=create)
    h.run()
    assert len(consumed_during_refresh) >= 70
    assert h.worker.reconnects == 0  # not torn down as "nothing usable"
    assert len(h.sources) == 2 and h.sources[1].resume == h.sources[0].checkpoint


def test_unexpected_mux_errors_are_contained(h, mocker):
    mocker.patch(f"{W}.mux_chunk", side_effect=RuntimeError("disk on fire"))
    h.resolutions = [resolved()]
    h.scripts = [run_of(100, 9, 4990.0) + ["end"]]
    assert h.run() == []  # no exception escapes start(), even from the final flush
    assert h.worker.chunks_failed == 2


@pytest.mark.parametrize("content", ["[]", "null", "5", '"x"', "{bad json", '{"stream_id": "abcdefghijk", "next_sequence": true}'])
def test_a_corrupt_state_file_is_ignored(h, tmp_path, content):
    (tmp_path / "key").mkdir(exist_ok=True)
    (tmp_path / "key" / "youtube_timeline_state.json").write_text(content)
    h.resolutions = [resolved()]
    h.run()
    assert h.sources[0].resume is None


def test_the_run_log_is_trimmed_during_long_runs(h, mocker):
    trims = mocker.patch(f"{W}.StreamHelper.trim_log_file")
    h.resolutions = [resolved()]
    h.scripts = [[None] * 10, ["end"]]
    h.worker._event("a")
    h.clock.sleep(400)
    h.worker._event("b")
    assert trims.call_count == 0  # no run active: no log file
    h.run()
    assert trims.call_count >= 2


def test_yt_dlp_never_runs_more_often_than_the_floor(h):
    """Even productive connections that drop quickly don't re-run yt-dlp faster
    than every 15 s."""
    h.resolutions = [resolved(), resolved(), resolved()]
    h.scripts = [run_of(100, 6, 4990.0) + [SourceExpiredError("403")], run_of(106, 1, 4990.0 + 6 * SEG) + [SourceExpiredError("403")]]
    h.run()
    gaps = [b - a for a, b in zip(h.resolve_calls, h.resolve_calls[1:], strict=False)]
    assert len(gaps) == 2 and all(g >= YouTubeTimelineWorker._MIN_RESOLVE_INTERVAL_SECONDS for g in gaps)


def test_failed_url_refreshes_are_retried_every_minute_not_continuously(h, mocker):
    expires = h.clock.wall() + 20 * 60 - 1
    calls = []

    def resolve(ytdlp_path, url, media_type, stop_event, log_file=None):
        calls.append(h.clock.t)
        if len(calls) == 1:
            return resolved(expires_at=expires)
        raise ResolveError("timed out")

    mocker.patch(f"{W}.resolve_stream", side_effect=resolve)

    def create(resolved_stream, resume, ctx):
        script = [group(s, 4990.0 + (s - 100) * SEG) for s in range(100, 400)] + ["end"]
        source = FakeSource(resolved_stream, resume, ctx, script, h.clock)
        h.sources.append(source)
        return source

    mocker.patch(f"{W}.create_segment_source", side_effect=create)
    h.run()
    refreshes = calls[1:]
    assert len(refreshes) >= 3
    assert all(b - a >= YouTubeTimelineWorker._PROACTIVE_RETRY_SECONDS for a, b in zip(refreshes, refreshes[1:], strict=False))


def _chunk_ending_at(clock_end, duration=6.0):
    from live_transcript_worker.youtube_timeline import Chunk

    return Chunk(1, 6, clock_end - duration, duration)


def test_clock_trust_bounds(h):
    """start + clock may run up to 900 s ahead of now, or behind by the catch-up
    window plus 300 s of delivery delay."""
    w = h.worker
    w._start_time = START
    now = START + 10_000.0
    ahead = YouTubeTimelineWorker._CLOCK_AHEAD_TOLERANCE_SECONDS
    behind = w.stale_lfs_gap_seconds + YouTubeTimelineWorker._CLOCK_BEHIND_TOLERANCE_SECONDS
    assert w._clock_trusted(_chunk_ending_at(10_000.0 + ahead - 1), now)
    assert not w._clock_trusted(_chunk_ending_at(10_000.0 + ahead + 1), now)
    assert w._clock_trusted(_chunk_ending_at(10_000.0 - behind + 1), now)
    assert not w._clock_trusted(_chunk_ending_at(10_000.0 - behind - 1), now)
    # A normal live chunk: a few seconds behind.
    assert w._clock_trusted(_chunk_ending_at(10_000.0 - 4), now)


def test_the_real_sleep_returns_promptly_on_stop(mocker):
    import threading
    import time

    mocker.patch("live_transcript_worker.worker_abstract.Config")
    stop = threading.Event()
    worker = YouTubeTimelineWorker("key", MagicMock(), stop)
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    worker._sleep(30)
    assert time.monotonic() - started < 1.5


def test_end_to_end_with_the_real_hls_source(h, mocker):
    """Drives the real HlsSegmentSource (against a fake YouTube) through the
    worker: lines are stamped start + YouTube clock + the HLS VOD offset."""
    from live_transcript_worker.youtube_timeline_source import HLS_CLOCK_TO_VOD_SECONDS, HlsSegmentSource
    from tests.test_youtube_timeline_source import FakeYouTube, SyncExecutor
    from tests.test_youtube_timeline_source import _clock as yt_clock

    yt = FakeYouTube(head=10)
    # Wall time just after segment 10 of the fake stream, so the clock is anchored.
    h.clock.wall_at_zero = START + yt_clock(10) + 3.0
    ticks = {"next": 1.0}
    real_sleep = h.clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        while h.clock.t >= ticks["next"]:
            ticks["next"] += 1.0
            for track in yt.head:
                yt.head[track] += 1
            if yt.head[TRACK_AUDIO] >= 22:
                yt.ended = True

    h.clock.sleep = sleep  # type: ignore[method-assign]
    h.worker._sleep = sleep  # type: ignore[method-assign]
    stream = ResolvedStream(
        "abcdefghijk",
        "is_live",
        tuple(ResolvedTrack(t, "m3u8_native", yt.playlist_url(t), yt.itag(t)) for t in (TRACK_VIDEO, TRACK_AUDIO)),
        None,
    )
    h.resolutions = [stream]

    def create(resolved_stream, resume, ctx):
        src = HlsSegmentSource(resolved_stream, resume, ctx, session_factory=lambda: yt, executor=SyncExecutor())
        src._monotonic = h.clock.mono  # type: ignore[method-assign]
        src._sleep = sleep  # type: ignore[method-assign]
        src._wait_slice = lambda future, seconds: sleep(seconds)  # type: ignore[method-assign]
        h.sources.append(src)  # type: ignore[arg-type]
        return src

    mocker.patch(f"{W}.create_segment_source", side_effect=create)
    items = h.run()
    assert len(items) >= 2
    first = items[0]
    assert first.raw == b"TS:10-15"
    assert first.vod_accurate is True
    assert first.audio_start_time == pytest.approx(START + yt_clock(10) + HLS_CLOCK_TO_VOD_SECONDS, abs=1e-4)
    assert HLS_CLOCK_TO_VOD_SECONDS == 0.036  # measured against a finished VOD
