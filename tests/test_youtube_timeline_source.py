import json
import re
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest
import requests

from live_transcript_worker.custom_types import Media
from live_transcript_worker.youtube_timeline import TRACK_AUDIO, TRACK_MUXED, TRACK_VIDEO
from live_transcript_worker.youtube_timeline_source import (
    HLS_CLOCK_TO_VOD_SECONDS,
    HlsSegmentSource,
    ResolvedStream,
    ResolvedTrack,
    ResolveError,
    ResolveStopped,
    SourceContext,
    SourceError,
    SourceExpiredError,
    SourceResetError,
    StreamNotLiveError,
    StreamUnavailableError,
    UnsupportedSourceError,
    build_resolve_command,
    classify_resolve,
    create_segment_source,
    format_args,
    parse_media_playlist,
    parse_resolved,
    resolve_stream,
    sequence_url_template,
    watch_url,
)
from tests.timeline_fixtures import audio_duration, audio_segment, ts_filler_packet, video_segment

MODULE = "live_transcript_worker.youtube_timeline_source"

# ----------------------------------------------------------------------
# Small pure functions
# ----------------------------------------------------------------------


def test_watch_url_pins_the_video_id():
    assert watch_url("https://www.youtube.com/@chan/live", "9Mvg2efaRgo") == "https://www.youtube.com/watch?v=9Mvg2efaRgo"
    assert watch_url("https://www.youtube.com/@chan/live", "Unknown ID") == "https://www.youtube.com/@chan/live"


def test_format_args_require_hls_and_skip_video_when_not_needed():
    video = format_args(Media.VIDEO)
    assert video[1] == "bv[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]"
    for media in (Media.AUDIO, Media.NONE):
        assert format_args(media)[1] == "ba[protocol^=m3u8]/b[protocol^=m3u8]"
    # Codec preferences stay a sort order, never a filter.
    assert video[2:] == ["-S", "vcodec:h264,proto:m3u8,acodec:aac"]


def test_build_resolve_command(mocker):
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=["--cookies", "c.txt"])
    cmd = build_resolve_command("/bin/yt-dlp", "https://www.youtube.com/watch?v=x", Media.VIDEO)
    assert cmd[:4] == ["/bin/yt-dlp", "-j", "--no-playlist", "--ignore-no-formats-error"]
    assert "--cookies" in cmd and cmd[-1] == "https://www.youtube.com/watch?v=x"


PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:8092
#EXT-X-DISCONTINUITY-SEQUENCE:1
#EXT-X-PROGRAM-DATE-TIME:2026-10-14T01:15:00.000+00:00
#EXTINF:1.0,
https://rr3.googlevideo.com/videoplayback/id/x.1/itag/234/playlist/index.m3u8/sq/8092/goap/l/dur/1.000/file/seg.ts
#EXTINF:0.998,
https://rr3.googlevideo.com/videoplayback/id/x.1/itag/234/playlist/index.m3u8/sq/8093/goap/l/dur/1.000/file/seg.ts
#EXTINF:1.0,
seg-8094.ts
"""


def test_parse_media_playlist():
    pl = parse_media_playlist(PLAYLIST, "https://host/path/index.m3u8")
    assert pl is not None
    assert pl.media_sequence == 8092 and pl.target_duration == 1.0 and not pl.ended
    assert [s[0] for s in pl.segments] == [8092, 8093, 8094]
    assert pl.segments[1][1] == pytest.approx(0.998)
    assert pl.segments[2][2] == "https://host/path/seg-8094.ts"
    assert (pl.first_sequence, pl.last_sequence) == (8092, 8094)


def test_parse_media_playlist_endlist_and_rejects():
    pl = parse_media_playlist(PLAYLIST + "#EXT-X-ENDLIST\n", "https://host/")
    assert pl is not None and pl.ended
    assert parse_media_playlist("<html>", "https://host/") is None
    assert parse_media_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n", "https://host/") is None
    empty = parse_media_playlist("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n", "https://host/")
    assert empty is not None and empty.last_sequence is None


def test_sequence_url_template():
    url = "https://rr3.googlevideo.com/videoplayback/id/x.1/itag/234/playlist/index.m3u8/sq/8092/goap/l%3D1/dur/1.000/file/seg.ts"
    template = sequence_url_template(url, 8092)
    assert template is not None
    assert template.format(294) == url.replace("/sq/8092/", "/sq/294/")
    # The sequence in the URL must match the playlist's, or the template could fetch the wrong segment.
    assert sequence_url_template(url, 8093) is None
    assert sequence_url_template("https://host/seg-1.ts", 1) is None


def test_sequence_url_template_escapes_braces():
    template = sequence_url_template("https://h/v/sq/7/x/{weird}/seg.ts", 7)
    assert template is not None and template.format(9) == "https://h/v/sq/9/x/{weird}/seg.ts"


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------

V_URL = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1791954750/id/x.1/itag/312/playlist/index.m3u8"
A_URL = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1791954700/id/x.1/itag/234/playlist/index.m3u8"


def _metadata(**overrides):
    meta = {
        "id": "x",
        "live_status": "is_live",
        "requested_formats": [
            {"format_id": "312", "protocol": "m3u8_native", "url": V_URL, "vcodec": "avc1.64002A", "acodec": "none",
             "http_headers": {"User-Agent": "UA"}},
            {"format_id": "234", "protocol": "m3u8_native", "url": A_URL, "vcodec": "none", "acodec": None,
             "http_headers": {"User-Agent": "UA"}},
        ],
    }  # fmt: skip
    meta.update(overrides)
    return meta


def test_parse_resolved_split_hls():
    r = parse_resolved(_metadata())
    assert [t.track for t in r.tracks] == [TRACK_VIDEO, TRACK_AUDIO]
    assert r.track(TRACK_AUDIO).format_id == "234"  # type: ignore[union-attr]
    assert r.tracks[0].http_headers == {"User-Agent": "UA"}
    assert r.tracks[1].raw["format_id"] == "234"  # kept for future transports
    assert r.expires_at == 1791954700  # the earliest expiry wins


def test_parse_resolved_query_style_expiry():
    meta = _metadata()
    for f in meta["requested_formats"]:
        f["url"] = "https://host/videoplayback?expire=1791950000&itag=1"
    assert parse_resolved(meta).expires_at == 1791950000


def test_parse_resolved_single_muxed_format():
    meta = {"id": "x", "live_status": "is_live", "format_id": "95", "protocol": "m3u8_native", "url": V_URL,
            "vcodec": "avc1", "acodec": "mp4a.40.2"}  # fmt: skip
    assert [t.track for t in parse_resolved(meta).tracks] == [TRACK_MUXED]


def test_parse_resolved_audio_only():
    meta = {"id": "x", "live_status": "is_live", "format_id": "234", "protocol": "m3u8_native", "url": A_URL,
            "vcodec": "none", "acodec": None}  # fmt: skip
    assert [t.track for t in parse_resolved(meta).tracks] == [TRACK_AUDIO]


@pytest.mark.parametrize("status", ["was_live", "post_live", "not_live", "is_upcoming"])
def test_parse_resolved_not_live(status):
    with pytest.raises(StreamNotLiveError):
        parse_resolved(_metadata(live_status=status))


def test_parse_resolved_without_a_live_status_is_retried_not_ended():
    """With --ignore-no-formats-error a bot check comes back as JSON without a
    live status; that must not end the run as 'not live'."""
    with pytest.raises(ResolveError) as excinfo:
        parse_resolved({"id": "x", "live_status": None, "formats": []}, "WARNING: [youtube] x: Sign in to confirm you're not a bot")
    assert not isinstance(excinfo.value, (StreamNotLiveError, UnsupportedSourceError))


def test_parse_resolved_rate_limited_live_stream_is_retried():
    with pytest.raises(ResolveError) as excinfo:
        parse_resolved({"id": "x", "live_status": "is_live", "formats": []}, "WARNING: This content isn't available, try again later")
    assert not isinstance(excinfo.value, UnsupportedSourceError)


def test_parse_resolved_unavailable_warning_with_exit_zero():
    with pytest.raises(StreamUnavailableError):
        classify_resolve(
            0,
            json.dumps({"id": "x", "live_status": "is_live", "formats": []}),
            "WARNING: Join this channel to get access to members-only content",
        )


def test_parse_resolved_no_selection_tells_why():
    """With --ignore-no-formats-error yt-dlp still prints the JSON when no HLS
    format matched; the reason decides between waiting and retrying."""
    sabr = {"id": "x", "live_status": "is_live", "formats": [{"format_id": "140", "protocol": "sabr"}]}
    with pytest.raises(UnsupportedSourceError, match="SABR"):
        parse_resolved(sabr)
    with pytest.raises(ResolveError) as excinfo:
        parse_resolved({"id": "x", "live_status": "is_live", "formats": []}, "WARNING: [youtube] x: n challenge solving failed")
    assert not isinstance(excinfo.value, UnsupportedSourceError)
    with pytest.raises(UnsupportedSourceError, match="no HLS"):
        parse_resolved({"id": "x", "live_status": "is_live", "formats": [{"protocol": "https"}]})


def test_parse_resolved_rejects_non_hls_selection():
    meta = _metadata()
    meta["requested_formats"][1]["protocol"] = "sabr"
    with pytest.raises(UnsupportedSourceError, match="SABR"):
        parse_resolved(meta)
    meta["requested_formats"][1]["protocol"] = "https"
    with pytest.raises(UnsupportedSourceError, match="not HLS"):
        parse_resolved(meta)


def test_parse_resolved_requires_audio():
    meta = _metadata()
    meta["requested_formats"] = meta["requested_formats"][:1]
    with pytest.raises(UnsupportedSourceError, match="no audio"):
        parse_resolved(meta)


@pytest.mark.parametrize(
    "returncode, stdout, stderr, error",
    [
        (1, "", "ERROR: [youtube] x: This live event has ended.", StreamNotLiveError),
        (1, "", "ERROR: [youtube:tab] chan is not currently live", StreamNotLiveError),
        (1, "", "ERROR: [youtube] x: Join this channel to get access to members-only content", StreamUnavailableError),
        (1, "", "ERROR: unable to download webpage: timed out", ResolveError),
        (0, "", "", StreamUnavailableError),  # skipped by --match-filter
        (0, "not json", "", ResolveError),
        (0, "[1, 2]", "", ResolveError),
    ],
)
def test_classify_resolve_errors(returncode, stdout, stderr, error):
    with pytest.raises(error):
        classify_resolve(returncode, stdout, stderr)


def test_classify_resolve_success():
    assert len(classify_resolve(0, json.dumps(_metadata()), "").tracks) == 2


class FakePopen:
    """Writes canned output into the files Popen was given; exits after `polls` polls."""

    instances: list["FakePopen"] = []

    def __init__(self, cmd, stdin=None, stdout=None, stderr=None, start_new_session=False, out="", err="", rc=0, polls=1):
        self.cmd = cmd
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self._polls = polls
        self._rc = rc
        stdout.write(out.encode())
        stderr.write(err.encode())
        FakePopen.instances.append(self)

    def poll(self):
        if self.killed:
            return self.returncode
        self._polls -= 1
        if self._polls <= 0:
            self.returncode = self._rc
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def popen(mocker):
    FakePopen.instances = []
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=[])
    mocker.patch(f"{MODULE}.os.killpg")
    mocker.patch(f"{MODULE}.time.sleep")

    def install(**kwargs):
        mocker.patch(f"{MODULE}.subprocess.Popen", side_effect=lambda cmd, **kw: FakePopen(cmd, **kw, **kwargs))

    return install


def _stop(values=None):
    stop = MagicMock()
    if values is None:
        stop.is_set.return_value = False
    else:
        stop.is_set.side_effect = values
    return stop


def test_resolve_stream_success_and_logs_stderr(popen):
    popen(out=json.dumps(_metadata()), err="[youtube] x: Downloading webpage\n", polls=3)
    log = MagicMock()
    r = resolve_stream("/bin/yt-dlp", "https://www.youtube.com/watch?v=x", Media.VIDEO, _stop(), log_file=log)
    assert len(r.tracks) == 2
    log.write.assert_called_once_with("[youtube] x: Downloading webpage\n")
    assert "--ignore-no-formats-error" in FakePopen.instances[0].cmd


def test_resolve_stream_stops_and_kills_on_stop(popen):
    popen(out="", polls=1000)
    with pytest.raises(ResolveStopped):
        resolve_stream("/bin/yt-dlp", "u", Media.VIDEO, _stop([False, False, True]))
    assert FakePopen.instances[0].killed


def test_resolve_stream_timeout_kills(popen, mocker):
    popen(out="", polls=1000)
    mocker.patch(f"{MODULE}.time.monotonic", side_effect=[0.0, 1.0, 200.0, 200.0])
    with pytest.raises(ResolveError, match="timed out"):
        resolve_stream("/bin/yt-dlp", "u", Media.VIDEO, _stop(), timeout=90)
    assert FakePopen.instances[0].killed


def test_resolve_stream_not_started(mocker):
    mocker.patch(f"{MODULE}.StreamHelper.ytdlp_auth_args", return_value=[])
    mocker.patch(f"{MODULE}.subprocess.Popen", side_effect=FileNotFoundError("yt-dlp"))
    with pytest.raises(ResolveError, match="could not be started"):
        resolve_stream("/bin/yt-dlp", "u", Media.VIDEO, _stop())


def _ctx(stop=None, max_lag=600.0):
    events: list[str] = []
    ctx = SourceContext(
        key="key",
        watch_url="u",
        ytdlp_path="yt-dlp",
        media_type=Media.VIDEO,
        stop_event=stop or _stop(),
        log=events.append,
        max_lag_seconds=max_lag,
    )
    return ctx, events


def test_create_segment_source_dispatch():
    ctx, _ = _ctx()
    hls = ResolvedStream("x", "is_live", (ResolvedTrack(TRACK_AUDIO, "m3u8_native", A_URL, "234"),), None)
    source = create_segment_source(hls, None, ctx)
    assert isinstance(source, HlsSegmentSource)
    assert source.vod_offset == HLS_CLOCK_TO_VOD_SECONDS and source.wrap_period
    source.close()
    sabr = ResolvedStream("x", "is_live", (ResolvedTrack(TRACK_AUDIO, "sabr", "u", "140"),), None)
    with pytest.raises(UnsupportedSourceError):
        create_segment_source(sabr, None, ctx)


# ----------------------------------------------------------------------
# HlsSegmentSource with a fake YouTube
# ----------------------------------------------------------------------

SEG = audio_duration(43)
BASE = "https://rr.googlevideo.com/videoplayback/itag/{itag}/playlist/index.m3u8/sq/{sq}/goap/lmt%3D{lmt}/dur/1.000/file/seg.ts"


def _clock(sq):
    return 1000.0 + sq * SEG


class FakeRaw:
    """urllib3-like raw stream: read1 returns at most `piece` bytes per call."""

    def __init__(self, body, piece, on_chunk):
        self._body = body
        self._pos = 0
        self._piece = piece
        self._on_chunk = on_chunk
        self.reads = 0

    @property
    def length_remaining(self):
        return len(self._body) - self._pos

    def read1(self, amt=None, decode_content=None):
        if self._pos >= len(self._body):
            return b""
        if self._on_chunk:
            self._on_chunk()
        self.reads += 1
        n = min(amt or len(self._body), self._piece)
        data = self._body[self._pos : self._pos + n]
        self._pos += n
        return data


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, on_chunk=None, piece=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._on_chunk = on_chunk
        self.closed = False
        if piece is not None:
            self.raw = FakeRaw(body, piece, on_chunk)

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            if self._on_chunk:
                self._on_chunk()
            yield self._body[i : i + size]

    def close(self):
        self.closed = True


class FakeYouTube:
    """Serves media playlists (a 3-segment window ending at `head`) and segments
    by sequence number, with scriptable failures. Also acts as the session."""

    def __init__(self, head=10, tracks=(TRACK_VIDEO, TRACK_AUDIO)):
        self.head = dict.fromkeys(tracks, head)
        self.ended = False
        self.window = 3
        # YouTube's segment URLs carry an `lmt` that can change mid-stream; a URL
        # with any other lmt is a 404. track -> [(first sequence, lmt), ...]
        self.lmts: dict[str, list[tuple[int, str]]] = {t: [(0, "33")] for t in tracks}
        self.status: dict[str, list[int]] = {}  # url substring -> statuses to return first
        self.headers_for: dict[int, dict] = {}
        self.raise_for: dict[str, list[Exception]] = {}
        self.missing: set[tuple[str, int]] = set()
        self.unreadable: set[str] = set()
        self.requests: list[str] = []
        self.on_chunk = None
        self.video_filler_packets = 0  # pads video segments to a realistic size
        self.piece: dict[str, int] = {}  # url substring -> bytes per read (raw.read1)
        self.playlist_on_chunk = None
        self.closed = False

    @staticmethod
    def itag(track):
        return {TRACK_VIDEO: "312", TRACK_AUDIO: "234", TRACK_MUXED: "95"}[track]

    def playlist_url(self, track):
        return f"https://manifest/{self.itag(track)}/index.m3u8"

    def lmt_for(self, track, sq):
        return [lmt for first, lmt in self.lmts[track] if sq >= first][-1]

    def change_lmt(self, from_sequence, lmt, tracks=None):
        for track in tracks or self.head:
            self.lmts[track].append((from_sequence, lmt))

    def _playlist(self, track):
        head = self.head[track]
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{head - self.window + 1}"]
        for sq in range(head - self.window + 1, head + 1):
            lines += ["#EXTINF:1.0,", BASE.format(itag=self.itag(track), sq=sq, lmt=self.lmt_for(track, sq))]
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def get(self, url, stream=False, timeout=None):
        self.requests.append(url)
        for key, errors in self.raise_for.items():
            if key in url and errors:
                raise errors.pop(0)
        for key, statuses in self.status.items():
            if key in url and statuses:
                status = statuses.pop(0)
                return FakeResponse(status, headers=self.headers_for.get(status))
        piece = next((n for key, n in self.piece.items() if key in url), None)
        for track in self.head:
            if url == self.playlist_url(track):
                return FakeResponse(200, self._playlist(track).encode(), on_chunk=self.playlist_on_chunk, piece=piece)
            if f"/itag/{self.itag(track)}/" in url:
                sq = int(url.split("/sq/")[1].split("/")[0])
                lmt = re.search(r"lmt%3D(\d+)", url)
                if (track, sq) in self.missing or sq > self.head[track] or (lmt and lmt.group(1) != self.lmt_for(track, sq)):
                    return FakeResponse(404)
                if track in self.unreadable:
                    return FakeResponse(200, b"<html>oops</html>")
                if track == TRACK_AUDIO:
                    data = audio_segment(_clock(sq))
                else:
                    data = video_segment(_clock(sq) + 0.01) + ts_filler_packet(0x100) * self.video_filler_packets
                return FakeResponse(200, data, on_chunk=self.on_chunk, piece=piece)
        return FakeResponse(404)

    def close(self):
        self.closed = True


class FakeTime:
    def __init__(self, yt=None, advance_head_every=None):
        self.t = 0.0
        self.yt = yt
        self.every = advance_head_every
        self._next_advance = advance_head_every

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        while self.yt is not None and self._next_advance is not None and self.t >= self._next_advance:
            for track in self.yt.head:
                self.yt.head[track] += 1
            self._next_advance += self.every


class SyncExecutor:
    """Runs fetches inline so tests are deterministic. `hold` keeps matching
    fetches pending forever (a download that never finishes)."""

    def __init__(self, hold=None):
        self.hold = hold or (lambda name, sequence: False)
        self.pending: list[Future] = []
        self.shut_down = False

    def submit(self, fn, *args):
        future: Future = Future()
        if self.hold(args[0], args[1]):
            self.pending.append(future)
            return future
        try:
            future.set_result(fn(*args))
        except BaseException as e:
            future.set_exception(e)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut_down = True


def _source(yt, resume=None, tracks=(TRACK_VIDEO, TRACK_AUDIO), stop=None, clock=None, executor=None, max_lag=600.0):
    resolved = ResolvedStream("x", "is_live", tuple(ResolvedTrack(t, "m3u8_native", yt.playlist_url(t), yt.itag(t)) for t in tracks), None)
    ctx, events = _ctx(stop, max_lag)
    src = HlsSegmentSource(resolved, resume, ctx, session_factory=lambda: yt, executor=executor or SyncExecutor())
    clock = clock or FakeTime()
    src._monotonic = clock.monotonic  # type: ignore[method-assign]
    src._sleep = clock.sleep  # type: ignore[method-assign]
    src._wait_slice = lambda future, seconds: clock.sleep(seconds)  # type: ignore[method-assign]
    return src, events, clock


def _playlist_requests(yt, track):
    return sum(1 for u in yt.requests if u == yt.playlist_url(track))


def test_source_starts_at_the_live_edge_and_pairs_tracks():
    yt = FakeYouTube(head=10)
    src, events, _ = _source(yt)
    group = src.next_group(5)
    assert group is not None and group.sequence == 10
    assert set(group.segments) == {TRACK_VIDEO, TRACK_AUDIO}
    assert group.segments[TRACK_AUDIO].clock == pytest.approx(_clock(10), abs=1e-4)
    assert src.checkpoint == 11
    assert any("live edge, sequence 10" in e for e in events)


def test_source_starts_at_the_edge_every_track_lists():
    yt = FakeYouTube(head=10)
    yt.head[TRACK_VIDEO] = 9
    src, _, _ = _source(yt)
    group = src.next_group(5)
    assert group is not None and group.sequence == 9


def test_source_polls_only_the_audio_playlist_after_opening():
    yt = FakeYouTube(head=10)
    clock = FakeTime(yt, advance_head_every=1.0)
    src, _, _ = _source(yt, clock=clock)
    for _ in range(5):
        assert src.next_group(5) is not None
    assert _playlist_requests(yt, TRACK_VIDEO) == 1  # once, for the URL template
    assert _playlist_requests(yt, TRACK_AUDIO) > 1


def test_source_waits_for_the_next_sequence_then_returns_none_on_timeout():
    yt = FakeYouTube(head=10)
    src, _, clock = _source(yt, resume=11)
    assert src.next_group(3) is None
    assert clock.t == pytest.approx(3, abs=1.01)
    yt.head = dict.fromkeys(yt.head, 11)
    group = src.next_group(3)
    assert group is not None and group.sequence == 11


def test_source_follows_a_growing_playlist():
    yt = FakeYouTube(head=10)
    clock = FakeTime(yt, advance_head_every=1.0)
    src, _, _ = _source(yt, clock=clock)
    sequences = [src.next_group(5).sequence for _ in range(5)]  # type: ignore[union-attr]
    assert sequences == [10, 11, 12, 13, 14]


def test_source_catches_up_by_number_after_falling_out_of_the_window():
    yt = FakeYouTube(head=20)
    src, events, _ = _source(yt, resume=12)
    group = src.next_group(5)
    assert group is not None and group.sequence == 12 and set(group.segments) == {TRACK_VIDEO, TRACK_AUDIO}
    assert any("resuming at sequence 12" in e for e in events)


def test_source_jumps_to_the_live_edge_beyond_the_catch_up_window():
    yt = FakeYouTube(head=100)
    src, events, _ = _source(yt, resume=10, max_lag=30.0)
    group = src.next_group(5)
    assert group is not None and group.sequence == 100
    assert src.segments_skipped == 90
    assert any("skipping 90 segments" in e for e in events)


def test_source_resume_before_a_stream_break_moves_to_the_oldest_available():
    """Segments from before a stream break return 404 by number: after a few in
    a row the source continues at the oldest segment the playlist lists."""
    yt = FakeYouTube(head=50)
    for sq in range(40, 48):
        yt.missing.add((TRACK_AUDIO, sq))
        yt.missing.add((TRACK_VIDEO, sq))
    src, events, _ = _source(yt, resume=40)
    groups = [src.next_group(5) for _ in range(HlsSegmentSource.BACKFILL_404_LIMIT)]
    assert [g.sequence for g in groups[:-1] if g] == [40, 41]
    assert groups[-1] is None  # the jump itself
    group = src.next_group(5)
    assert group is not None and group.sequence == 48 and TRACK_AUDIO in group.segments
    assert any("no longer available" in e for e in events)


def test_source_missing_video_segment_still_returns_audio_and_logs_the_streak_once():
    yt = FakeYouTube(head=10)
    clock = FakeTime(yt, advance_head_every=1.0)
    for sq in (10, 11, 12):
        yt.missing.add((TRACK_VIDEO, sq))
    src, events, _ = _source(yt, clock=clock)
    groups = [src.next_group(5) for _ in range(4)]
    assert [set(g.segments) for g in groups] == [{TRACK_AUDIO}] * 3 + [{TRACK_AUDIO, TRACK_VIDEO}]  # type: ignore[union-attr]
    assert src.segments_missing == 3
    assert src.consecutive_missing == {TRACK_AUDIO: 0, TRACK_VIDEO: 0}
    video_events = [e for e in events if e.startswith("video")]
    assert len(video_events) == 2  # the first miss, then the streak summary
    assert "sequences 10-12 were missing" in video_events[1]


def test_source_unreadable_audio_is_missing():
    yt = FakeYouTube(head=10)
    yt.unreadable.add(TRACK_AUDIO)
    src, events, _ = _source(yt)
    group = src.next_group(5)
    assert group is not None and set(group.segments) == {TRACK_VIDEO}
    assert any("unreadable" in e for e in events)


def test_source_audio_403_means_expired_but_video_403_is_only_missing():
    yt = FakeYouTube(head=10)
    yt.status["/itag/234/"] = [403]
    src, _, _ = _source(yt)
    with pytest.raises(SourceExpiredError):
        src.next_group(5)

    yt = FakeYouTube(head=10)
    yt.status["/itag/312/"] = [403]
    src, events, _ = _source(yt)
    group = src.next_group(5)
    assert group is not None and set(group.segments) == {TRACK_AUDIO}
    assert any("HTTP 403" in e for e in events)


def test_source_playlist_404_means_expired():
    yt = FakeYouTube(head=10)
    yt.status["manifest/234"] = [404, 404]
    src, _, _ = _source(yt)
    with pytest.raises(SourceExpiredError, match="gone"):
        src.next_group(5)


def test_source_retries_transient_errors():
    yt = FakeYouTube(head=10)
    yt.raise_for["/itag/312/"] = [requests.ConnectionError("reset")]
    yt.status["/itag/234/"] = [503, 429]
    yt.headers_for[429] = {"Retry-After": "7"}
    src, _, clock = _source(yt)
    group = src.next_group(15)
    assert group is not None and set(group.segments) == {TRACK_VIDEO, TRACK_AUDIO}
    assert clock.t >= 7.0  # honoured Retry-After (the default backoff would be 2 s)


def test_source_edge_404_is_retried_until_the_segment_appears():
    yt = FakeYouTube(head=10)
    yt.status["/itag/234/playlist/index.m3u8/sq/10/"] = [404, 404]
    src, _, _ = _source(yt)
    group = src.next_group(5)
    assert group is not None and TRACK_AUDIO in group.segments


def test_source_audio_failing_repeatedly_raises_without_consuming_the_sequence():
    yt = FakeYouTube(head=10)
    yt.raise_for["/itag/234/"] = [requests.Timeout("slow")] * 1000
    src, _, _ = _source(yt)
    for _ in range(HlsSegmentSource.MAX_CONSECUTIVE_FETCH_FAILURES - 1):
        assert src.next_group(5) is None  # retried on the next call
        assert src.checkpoint == 10
    with pytest.raises(SourceError):
        src.next_group(5)
    assert src.checkpoint == 10


def test_source_video_failing_repeatedly_never_raises():
    yt = FakeYouTube(head=10)
    yt.raise_for["/itag/312/"] = [requests.Timeout("slow")] * 1000
    clock = FakeTime(yt, advance_head_every=1.0)
    src, _, _ = _source(yt, clock=clock)
    for i in range(10):
        group = src.next_group(30)
        assert group is not None and set(group.segments) == {TRACK_AUDIO}
        assert src.consecutive_missing[TRACK_VIDEO] == i + 1


def test_source_trickling_download_hits_its_deadline():
    yt = FakeYouTube(head=10)
    yt.video_filler_packets = 1500  # ~280 KB
    clock = FakeTime()
    yt.on_chunk = lambda: clock.sleep(5.0)  # every 64 KiB takes 5 s
    src, _, _ = _source(yt, tracks=(TRACK_VIDEO, TRACK_AUDIO), clock=clock)
    group = src.next_group(60)
    # The 16 KiB audio segment fits in one chunk; the large video segment can't finish in time.
    assert group is not None and set(group.segments) == {TRACK_AUDIO}


def test_source_late_video_is_left_out_after_the_grace_period():
    yt = FakeYouTube(head=10)
    executor = SyncExecutor(hold=lambda name, sequence: name == TRACK_VIDEO)
    src, events, clock = _source(yt, executor=executor)
    group = src.next_group(10)
    assert group is not None and set(group.segments) == {TRACK_AUDIO}
    assert clock.t <= 10
    assert executor.pending[0].cancelled()
    assert any("too late" in e for e in events)


def test_source_late_video_within_grace_returns_none_first():
    yt = FakeYouTube(head=10)
    executor = SyncExecutor(hold=lambda name, sequence: name == TRACK_VIDEO)
    src, _, _ = _source(yt, executor=executor)
    assert src.next_group(0.5) is None  # grace (2 s) not over yet
    group = src.next_group(5)
    assert group is not None and group.sequence == 10


def test_source_detects_endlist():
    yt = FakeYouTube(head=10)
    src, _, _ = _source(yt, resume=11)
    yt.ended = True
    assert src.next_group(5) is None
    assert src.ended


def test_source_detects_a_sequence_reset():
    yt = FakeYouTube(head=11000)
    src, _, clock = _source(yt, resume=11001)
    yt.head = dict.fromkeys(yt.head, 30)  # numbering restarted: 11000 is gone too
    with pytest.raises(SourceResetError):
        src.next_group(30)
    # It takes several polls over at least 10 s, not a single stale response.
    assert clock.t >= HlsSegmentSource.RESET_MIN_SECONDS


def test_source_stale_playlist_is_not_mistaken_for_a_reset():
    """An edge node serving an old playlist after a reconnect: the last delivered
    sequence is still served by number, so it's only stale."""
    yt = FakeYouTube(head=110)
    src, events, clock = _source(yt, resume=111)
    yt.head = dict.fromkeys(yt.head, 100)  # stale playlist...
    original = yt.get

    def get(url, stream=False, timeout=None):
        if "/sq/110/" in url:  # ...but the segment it doesn't list yet still exists
            return FakeResponse(200, audio_segment(_clock(110)))
        return original(url, stream, timeout)

    yt.get = get  # type: ignore[method-assign]
    assert src.next_group(20) is None
    assert any("playlist is stale" in e for e in events)
    yt.head = dict.fromkeys(yt.head, 111)
    group = src.next_group(20)
    assert group is not None and group.sequence == 111


def test_source_a_single_stale_poll_never_resets():
    yt = FakeYouTube(head=110)
    src, _, _ = _source(yt, resume=111)
    yt.head = dict.fromkeys(yt.head, 100)
    assert src.next_group(1.5) is None
    yt.head = dict.fromkeys(yt.head, 111)
    assert src.next_group(5) is not None


def test_source_stop_event_interrupts_waiting():
    yt = FakeYouTube(head=10)
    stop = _stop([False] * 4 + [True] * 1000)
    src, _, clock = _source(yt, resume=11, stop=stop)
    assert src.next_group(60) is None
    assert clock.t < 60


def test_source_audio_only_stream():
    yt = FakeYouTube(head=10, tracks=(TRACK_AUDIO,))
    src, _, _ = _source(yt, tracks=(TRACK_AUDIO,))
    group = src.next_group(5)
    assert group is not None and set(group.segments) == {TRACK_AUDIO}
    assert src.clock_track == TRACK_AUDIO


def test_source_without_a_video_template_polls_the_video_playlist(mocker):
    yt = FakeYouTube(head=10)
    clock = FakeTime(yt, advance_head_every=1.0)
    real = sequence_url_template
    mocker.patch(f"{MODULE}.sequence_url_template", side_effect=lambda url, sq: None if "/itag/312/" in url else real(url, sq))
    src, _, _ = _source(yt, clock=clock)
    for _ in range(3):
        group = src.next_group(5)
        assert group is not None and set(group.segments) == {TRACK_VIDEO, TRACK_AUDIO}
    assert _playlist_requests(yt, TRACK_VIDEO) > 1


def test_source_close_cancels_pending_fetches_and_closes_sessions():
    yt = FakeYouTube(head=10)
    executor = SyncExecutor(hold=lambda name, sequence: name == TRACK_VIDEO)
    src, _, _ = _source(yt, executor=executor)
    assert src.next_group(0.5) is None
    src.close()
    assert executor.shut_down and yt.closed
    assert all(f.cancelled() for f in executor.pending)
    assert src.next_group(5) is None  # closed: returns at once


def test_source_a_broken_video_playlist_never_stops_audio():
    for status in (503, 404, 403):
        yt = FakeYouTube(head=10)
        yt.status["manifest/312"] = [status] * 1000
        clock = FakeTime(yt, advance_head_every=1.0)
        src, events, _ = _source(yt, clock=clock)
        groups = [src.next_group(30) for _ in range(5)]
        assert all(g is not None and set(g.segments) == {TRACK_AUDIO} for g in groups), status
        assert any("video playlist unavailable" in e for e in events)
        # The broken playlist is retried, but rarely.
        assert sum(1 for u in yt.requests if "manifest/312" in u) <= 3 * 3


def test_source_trickling_playlist_hits_its_deadline_and_stop_is_honoured():
    yt = FakeYouTube(head=10)
    clock = FakeTime()
    yt.piece["manifest/234"] = 100  # ~400 B playlist in 100 B reads...
    yt.playlist_on_chunk = lambda: clock.sleep(2.0)  # ...2 s apart
    src, _, _ = _source(yt, clock=clock)
    with pytest.raises(SourceError):
        src.next_group(60)
    # 3 attempts of a 4 s deadline plus backoff, not the full trickle each time.
    assert clock.t < 3 * (4 + 2) + 4

    stop = _stop()
    yt = FakeYouTube(head=10)
    clock = FakeTime()
    yt.piece["manifest/234"] = 100

    def trickle():
        clock.sleep(2.0)
        stop.is_set.return_value = True

    yt.playlist_on_chunk = trickle
    src, _, _ = _source(yt, clock=clock, stop=stop)
    assert src.next_group(60) is None
    assert clock.t <= 2.0


def test_source_a_body_that_completes_late_is_kept():
    yt = FakeYouTube(head=10)
    clock = FakeTime()
    yt.piece["/itag/234/"] = 4096  # a 16 KB audio segment in 4 reads
    yt.on_chunk = lambda: clock.sleep(2.5)  # the last read lands after the 8 s deadline
    src, _, _ = _source(yt, tracks=(TRACK_AUDIO,), clock=clock)
    group = src.next_group(60)
    assert group is not None and TRACK_AUDIO in group.segments


def test_source_audio_is_not_starved_by_stalled_video_downloads():
    """Real threads: every video request hangs, audio is instant. Audio keeps
    flowing because each track has its own download pool."""
    import threading
    import time

    from live_transcript_worker.youtube_timeline_source import HlsSegmentSource as Real

    yt = FakeYouTube(head=10)
    release = threading.Event()
    original = yt.get

    def get(url, stream=False, timeout=None):
        if "/itag/312/" in url:
            release.wait(10)
        return original(url, stream, timeout)

    yt.get = get  # type: ignore[method-assign]
    resolved = ResolvedStream(
        "x", "is_live", tuple(ResolvedTrack(t, "m3u8_native", yt.playlist_url(t), yt.itag(t)) for t in (TRACK_VIDEO, TRACK_AUDIO)), None
    )
    ctx, _ = _ctx()
    src = Real(resolved, None, ctx, session_factory=lambda: yt)
    src.VIDEO_GRACE_TARGETS = 0.2  # keep the test short
    try:
        started = time.monotonic()
        sequences = []
        while len(sequences) < 6 and time.monotonic() - started < 8:
            yt.head = dict.fromkeys(yt.head, yt.head[TRACK_AUDIO] + 1)
            g = src.next_group(2)
            if g is not None:
                assert TRACK_AUDIO in g.segments
                sequences.append(g.sequence)
        assert len(sequences) == 6
    finally:
        release.set()
        src.close()


def test_source_abandoned_video_download_stops_reading():
    """A video segment dropped as too late stops downloading instead of
    holding a thread and bandwidth."""
    import time

    from live_transcript_worker.youtube_timeline_source import HlsSegmentSource as Real

    yt = FakeYouTube(head=10)
    yt.video_filler_packets = 20_000  # ~3.7 MB
    yt.piece["/itag/312/"] = 1024
    yt.on_chunk = lambda: time.sleep(0.01)  # ~100 KB/s
    raws = []
    original = yt.get

    def get(url, stream=False, timeout=None):
        resp = original(url, stream, timeout)
        if "/itag/312/" in url and hasattr(resp, "raw"):
            raws.append(resp.raw)
        return resp

    yt.get = get  # type: ignore[method-assign]
    resolved = ResolvedStream(
        "x", "is_live", tuple(ResolvedTrack(t, "m3u8_native", yt.playlist_url(t), yt.itag(t)) for t in (TRACK_VIDEO, TRACK_AUDIO)), None
    )
    ctx, _ = _ctx()
    src = Real(resolved, None, ctx, session_factory=lambda: yt)
    try:
        group = None
        while group is None:
            group = src.next_group(5)
        assert set(group.segments) == {TRACK_AUDIO}
        time.sleep(0.3)
        reads = [r.reads for r in raws]
        time.sleep(0.5)
        assert [r.reads for r in raws] == reads  # nothing reads any more
    finally:
        src.close()


def test_source_video_grace_runs_from_when_the_audio_arrived():
    """Video that never arrives must not slow audio below real time: audio
    prefetched earlier has already used up its grace period."""
    yt = FakeYouTube(head=13)
    executor = SyncExecutor(hold=lambda name, sequence: name == TRACK_VIDEO)
    src, _, clock = _source(yt, resume=10, executor=executor)
    groups = []
    while len(groups) < 4 and clock.t < 30:
        g = src.next_group(5)
        if g is not None:
            groups.append(g)
    assert [g.sequence for g in groups] == [10, 11, 12, 13]
    assert all(set(g.segments) == {TRACK_AUDIO} for g in groups)
    # One grace period (2 s) for all four, not one each.
    assert clock.t < 3.0


def test_source_backfill_404s_are_counted_per_sequence_not_per_call():
    """Revisiting the same missing older segment (waiting on its video) must not
    count as several missing segments and trigger a jump past available ones."""
    yt = FakeYouTube(head=50)
    yt.missing.add((TRACK_AUDIO, 40))
    executor = SyncExecutor(hold=lambda name, sequence: name == TRACK_VIDEO and sequence == 40)
    src, events, _ = _source(yt, resume=40, executor=executor)
    calls, group = 0, None
    while group is None:
        calls += 1
        group = src.next_group(0.3)  # sequence 40 is revisited while its video is awaited
    assert calls > HlsSegmentSource.BACKFILL_404_LIMIT
    assert group.sequence == 40 and TRACK_AUDIO not in group.segments
    group = src.next_group(5)
    assert group is not None and group.sequence == 41 and TRACK_AUDIO in group.segments
    assert not any("no longer available" in e for e in events)


def test_source_rate_limited_playlist_keeps_next_group_within_its_timeout():
    yt = FakeYouTube(head=10)
    src, _, clock = _source(yt, resume=10)
    assert src.next_group(5) is not None  # opened, sequence 10 delivered
    yt.status["manifest/234"] = [429] * 100
    yt.headers_for[429] = {"Retry-After": "30"}
    started = clock.t
    assert src.next_group(2) is None
    assert clock.t - started <= 2 + 1  # no 30 s Retry-After wait inside the call
    playlist_requests = _playlist_requests(yt, TRACK_AUDIO)
    assert src.next_group(2) is None
    assert _playlist_requests(yt, TRACK_AUDIO) - playlist_requests <= 1  # not hammering either


# ----------------------------------------------------------------------
# URL templates going stale (seen in production: video lost after ~7 min)
# ----------------------------------------------------------------------


def test_source_template_comes_from_the_newest_segment():
    """A DVR playlist spans minutes; its oldest segments can carry an lmt from
    before a mid-stream change. The template must follow the newest URL, or
    every video segment fetched by number is a 404."""
    yt = FakeYouTube(head=110)
    yt.window = 30
    yt.change_lmt(105, "34")
    clock = FakeTime(yt, advance_head_every=1.0)
    src, events, _ = _source(yt, clock=clock)
    groups = [src.next_group(10) for _ in range(6)]
    assert [g.sequence for g in groups] == list(range(110, 116))  # type: ignore[union-attr]
    # 111+ aren't in the playlist read at open: built from the template, with the current lmt.
    assert all(set(g.segments) == {TRACK_VIDEO, TRACK_AUDIO} for g in groups)  # type: ignore[union-attr]


def test_source_follows_an_lmt_change_mid_stream():
    """The production failure: YouTube changes lmt while the worker runs. The
    audio playlist shows it; the video template is refreshed before it's used."""
    yt = FakeYouTube(head=10)
    yt.change_lmt(15, "34")
    clock = FakeTime(yt, advance_head_every=1.0)
    src, events, _ = _source(yt, clock=clock)
    groups = [src.next_group(10) for _ in range(10)]
    assert [g.sequence for g in groups] == list(range(10, 20))  # type: ignore[union-attr]
    missing_video = [g.sequence for g in groups if TRACK_VIDEO not in g.segments]  # type: ignore[union-attr]
    assert missing_video == []
    assert any("segment URLs changed" in e for e in events)


def test_source_video_404s_refresh_a_stale_template():
    """Safety net: only the video URLs change (the audio playlist can't show it).
    The 404s mark the template stale and the video playlist is re-read."""
    yt = FakeYouTube(head=10)
    yt.change_lmt(13, "34", tracks=[TRACK_VIDEO])
    clock = FakeTime(yt, advance_head_every=1.0)
    src, events, _ = _source(yt, clock=clock)
    groups = [src.next_group(15) for _ in range(12)]
    missing_video = [g.sequence for g in groups if TRACK_VIDEO not in g.segments]  # type: ignore[union-attr]
    assert 0 < len(missing_video) <= 2  # the first refusal(s), then recovered
    assert TRACK_VIDEO in groups[-1].segments  # type: ignore[union-attr]
    assert any("video sequence 13" in e and "404" in e for e in events)


def test_too_late_names_what_the_download_keeps_getting():
    """'too late' used to hide a stream of 404s from a stale URL."""

    class NotingExecutor(SyncExecutor):
        def submit(self, fn, *args):
            if args[0] == TRACK_VIDEO:
                args[-1][0] = "HTTP 404"  # the fetch's note: it keeps getting 404s
                future: Future = Future()
                self.pending.append(future)
                return future
            return super().submit(fn, *args)

    yt = FakeYouTube(head=10)
    src, events, _ = _source(yt, executor=NotingExecutor())
    group = src.next_group(10)
    assert group is not None and set(group.segments) == {TRACK_AUDIO}
    assert any("too late (still retrying after HTTP 404)" in e for e in events)
    # Sequence 10's URL came from the playlist itself: a 404 there isn't a template problem.
    assert not src._tracks[TRACK_VIDEO].stale
    yt.head[TRACK_AUDIO] = 11  # 11 isn't in the video playlist read at open: built from the template
    group = src.next_group(10)
    assert group is not None and group.sequence == 11 and set(group.segments) == {TRACK_AUDIO}
    assert src._tracks[TRACK_VIDEO].stale  # so the video playlist is re-read before the next one
