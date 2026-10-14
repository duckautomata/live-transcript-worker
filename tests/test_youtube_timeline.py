import io
import shutil
import subprocess

import av
import pytest

from live_transcript_worker.youtube_timeline import (
    CONTAINER_ADTS,
    CONTAINER_FMP4,
    CONTAINER_MPEGTS,
    TRACK_AUDIO,
    TRACK_MUXED,
    TRACK_VIDEO,
    TS_WRAP_SECONDS,
    ChunkAssembler,
    ChunkTrack,
    ClockUnwrapper,
    MuxError,
    SegmentGroup,
    TrackSegment,
    build_mux_command,
    detect_container,
    mux_chunk,
    parse_adts,
    parse_id3_timestamp,
    scan_ts_pts,
)
from tests.timeline_fixtures import (
    adts_frame,
    audio_duration,
    audio_segment,
    id3_timestamp_tag,
    ts_filler_packet,
    ts_pes_packet,
    video_segment,
)

# ----------------------------------------------------------------------
# Parsers
# ----------------------------------------------------------------------


def test_id3_timestamp_round_trips():
    assert parse_id3_timestamp(audio_segment(2001.188567)) == pytest.approx(2001.188567, abs=1e-5)


def test_id3_timestamp_masks_to_33_bits():
    """Only the low 33 bits are the clock; the upper bits of the 8-byte field are ignored."""
    tag = bytearray(id3_timestamp_tag(5.0))
    tag[-8] |= 0x80  # set a high bit outside the 33-bit clock
    assert parse_id3_timestamp(bytes(tag)) == pytest.approx(5.0)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"ID3",
        adts_frame() * 3,  # no tag at all
        b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"TIT2\x00\x00\x00\x02\x00\x00\x00x",  # tag without the PRIV frame
    ],
)
def test_id3_timestamp_missing(data):
    assert parse_id3_timestamp(data) is None


def test_id3_timestamp_ignores_owner_string_outside_the_tag():
    """The owner string appearing later in the payload must not be mistaken for the tag."""
    data = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 4 + id3_timestamp_tag(9.0)[10:]
    assert parse_id3_timestamp(data) is None


def test_parse_adts_counts_frames_and_duration():
    info = parse_adts(audio_segment(1.0, frames=43))
    assert info is not None
    assert info.frames == 43
    assert info.sample_rate == 44100
    assert info.duration == pytest.approx(audio_duration(43))


def test_parse_adts_skips_id3_between_frames():
    """Concatenated segments carry an ID3 tag at every segment start."""
    data = audio_segment(1.0, frames=10) + audio_segment(2.0, frames=12)
    info = parse_adts(data)
    assert info is not None and info.frames == 22


def test_parse_adts_ignores_truncated_last_frame():
    data = audio_segment(1.0, frames=5) + adts_frame()[:10]
    info = parse_adts(data)
    assert info is not None and info.frames == 5


def test_parse_adts_counts_raw_data_blocks():
    info = parse_adts(adts_frame(blocks=1) + adts_frame(blocks=0))
    assert info is not None and info.samples == 1024 * 2 + 1024


def test_parse_adts_rejects_garbage_and_mixed_rates():
    assert parse_adts(b"\x00" * 100) is None
    assert parse_adts(id3_timestamp_tag(1.0)) is None
    assert parse_adts(adts_frame(sr_index=4) + adts_frame(sr_index=3)) is None


def test_scan_ts_pts_video_and_audio():
    data = ts_pes_packet(0x100, 0xE0, 100.5) + ts_pes_packet(0x101, 0xC0, 100.48) + ts_filler_packet(0x100)
    info = scan_ts_pts(data)
    assert info is not None
    assert info.video_pts == pytest.approx(100.5)
    assert info.audio_pts == pytest.approx(100.48)


def test_scan_ts_pts_takes_earliest_presentation_time():
    """B-frames make later PES carry earlier PTS; the segment starts at the earliest."""
    data = ts_pes_packet(0x100, 0xE0, 10.1) + ts_pes_packet(0x100, 0xE0, 10.0) + ts_pes_packet(0x100, 0xE0, 10.2)
    info = scan_ts_pts(data)
    assert info is not None and info.video_pts == pytest.approx(10.0)


def test_scan_ts_pts_earliest_across_a_wrap():
    data = ts_pes_packet(0x100, 0xE0, TS_WRAP_SECONDS - 0.05) + ts_pes_packet(0x100, 0xE0, 0.01)
    info = scan_ts_pts(data)
    assert info is not None and info.video_pts == pytest.approx(TS_WRAP_SECONDS - 0.05)


def test_scan_ts_pts_resyncs_after_garbage():
    data = ts_pes_packet(0x100, 0xE0, 3.0) + b"\x00" * 50 + ts_pes_packet(0x100, 0xE0, 2.0) + ts_filler_packet(0x100)
    info = scan_ts_pts(data)
    assert info is not None and info.video_pts == pytest.approx(2.0)


def test_scan_ts_pts_none_for_non_ts():
    assert scan_ts_pts(b"") is None
    assert scan_ts_pts(audio_segment(1.0)) is None
    assert scan_ts_pts(ts_filler_packet(0x100) * 3) is None


def test_detect_container():
    assert detect_container(audio_segment(1.0)) == CONTAINER_ADTS
    assert detect_container(adts_frame() * 2) == CONTAINER_ADTS
    assert detect_container(video_segment(1.0)) == CONTAINER_MPEGTS
    assert detect_container(b"\x00\x00\x00\x18moof" + b"\x00" * 16) == CONTAINER_FMP4
    assert detect_container(b"<html>not media</html>") is None
    assert detect_container(b"") is None


# ----------------------------------------------------------------------
# Clock
# ----------------------------------------------------------------------


def test_unwrapper_is_monotonic_across_the_wrap():
    u = ClockUnwrapper()
    assert u.unwrap(TS_WRAP_SECONDS - 1.0) == pytest.approx(TS_WRAP_SECONDS - 1.0)
    assert not u.wrapped
    assert u.unwrap(0.5) == pytest.approx(TS_WRAP_SECONDS + 0.5)
    assert u.wrapped
    assert u.unwrap(1.5) == pytest.approx(TS_WRAP_SECONDS + 1.5)
    assert not u.wrapped


def test_unwrapper_maps_a_late_pre_wrap_value_back_without_moving():
    u = ClockUnwrapper()
    u.unwrap(TS_WRAP_SECONDS - 1.0)
    u.unwrap(0.5)  # wrapped
    assert u.unwrap(TS_WRAP_SECONDS - 0.5) == pytest.approx(TS_WRAP_SECONDS - 0.5)
    # The next normal value is not counted as a second wrap.
    assert u.unwrap(2.5) == pytest.approx(TS_WRAP_SECONDS + 2.5)


# ----------------------------------------------------------------------
# ChunkAssembler
# ----------------------------------------------------------------------

AUDIO_SEG = audio_duration(43)


def _group(sequence, clock, video=True, audio=True, duration=AUDIO_SEG):
    segments = {}
    if audio:
        segments[TRACK_AUDIO] = TrackSegment(TRACK_AUDIO, sequence, b"a%d" % sequence, CONTAINER_ADTS, clock, duration)
    if video:
        segments[TRACK_VIDEO] = TrackSegment(TRACK_VIDEO, sequence, b"v%d" % sequence, CONTAINER_MPEGTS, clock + 0.01, 1.0)
    return SegmentGroup(sequence, segments)


def _feed(assembler, groups):
    out = []
    for g in groups:
        out += assembler.add(g)
    return out


def test_assembler_emits_contiguous_chunks_of_target_duration():
    asm = ChunkAssembler(6, TRACK_AUDIO)
    chunks = _feed(asm, [_group(100 + i, 50.0 + i * AUDIO_SEG) for i in range(13)])
    assert [(c.first_sequence, c.last_sequence) for c in chunks] == [(100, 105), (106, 111)]
    assert chunks[0].clock == pytest.approx(50.0)
    assert chunks[1].clock == pytest.approx(50.0 + 6 * AUDIO_SEG)
    assert chunks[0].duration == pytest.approx(6 * AUDIO_SEG)
    assert chunks[0].tracks[TRACK_AUDIO].data == b"a100a101a102a103a104a105"
    assert chunks[0].tracks[TRACK_AUDIO].clock == pytest.approx(50.0)
    assert chunks[0].tracks[TRACK_VIDEO].data == b"v100v101v102v103v104v105"
    assert asm.pending_duration == pytest.approx(AUDIO_SEG)


def test_assembler_closes_at_the_boundary_nearest_the_target():
    """6 s target: 1 s and 2 s segments give 6 s chunks; 5 s segments give 5 s chunks, not 10 s."""
    # 4 s segments tie between 4 s and 8 s; the shorter wins.
    for seg_seconds, per_chunk in ((AUDIO_SEG, 6), (2.0, 3), (3.0, 2), (5.0, 1), (4.0, 1)):
        asm = ChunkAssembler(6, TRACK_AUDIO)
        chunks = _feed(asm, [_group(i, 10.0 + i * seg_seconds, duration=seg_seconds) for i in range(12)])
        assert {c.last_sequence - c.first_sequence + 1 for c in chunks} == {per_chunk}, seg_seconds


def test_assembler_breaks_on_sequence_jump():
    gaps = []
    asm = ChunkAssembler(6, TRACK_AUDIO, on_gap=gaps.append)
    groups = [_group(1, 10.0), _group(2, 10.0 + AUDIO_SEG), _group(20, 10.0 + 19 * AUDIO_SEG), _group(21, 10.0 + 20 * AUDIO_SEG)]
    chunks = _feed(asm, groups)
    assert [(c.first_sequence, c.last_sequence) for c in chunks] == [(1, 2)]
    rest = asm.flush()
    assert rest is not None and (rest.first_sequence, rest.last_sequence) == (20, 21)
    # The new chunk starts at its own clock: nothing carried over from before the gap.
    assert rest.clock == pytest.approx(10.0 + 19 * AUDIO_SEG)
    assert len(gaps) == 1 and "sequence jumped" in gaps[0]


def test_assembler_breaks_on_clock_discontinuity_with_contiguous_sequences():
    gaps = []
    asm = ChunkAssembler(6, TRACK_AUDIO, on_gap=gaps.append)
    chunks = _feed(asm, [_group(1, 10.0), _group(2, 10.0 + AUDIO_SEG), _group(3, 75.0)])
    assert [(c.first_sequence, c.last_sequence) for c in chunks] == [(1, 2)]
    rest = asm.flush()
    assert rest is not None and rest.clock == pytest.approx(75.0)
    assert "clock jumped" in gaps[0]


def test_assembler_tolerates_small_clock_jitter():
    asm = ChunkAssembler(6, TRACK_AUDIO)
    chunks = _feed(asm, [_group(1, 10.0), _group(2, 10.0 + AUDIO_SEG + 0.004)])
    assert chunks == []
    assert asm.flush().last_sequence == 2  # type: ignore[union-attr]


def test_assembler_missing_audio_closes_the_chunk_and_drops_the_group():
    gaps = []
    asm = ChunkAssembler(6, TRACK_AUDIO, on_gap=gaps.append)
    chunks = _feed(asm, [_group(1, 10.0), _group(2, 10.0 + AUDIO_SEG, audio=False), _group(3, 10.0 + 2 * AUDIO_SEG)])
    assert [(c.first_sequence, c.last_sequence) for c in chunks] == [(1, 1)]
    assert b"v2" not in chunks[0].tracks[TRACK_VIDEO].data
    rest = asm.flush()
    # Sequence 3 starts a fresh chunk at its own clock (not glued to sequence 1).
    assert rest is not None and rest.first_sequence == 3 and rest.clock == pytest.approx(10.0 + 2 * AUDIO_SEG)
    assert len(gaps) == 1


def test_assembler_missing_video_keeps_the_audio_run():
    asm = ChunkAssembler(6, TRACK_AUDIO)
    _feed(asm, [_group(1, 10.0), _group(2, 10.0 + AUDIO_SEG, video=False), _group(3, 10.0 + 2 * AUDIO_SEG)])
    chunk = asm.flush()
    assert chunk is not None and (chunk.first_sequence, chunk.last_sequence) == (1, 3)
    assert chunk.tracks[TRACK_AUDIO].data == b"a1a2a3"
    assert chunk.tracks[TRACK_VIDEO].data == b"v1v3"


def test_assembler_chunk_without_any_video():
    asm = ChunkAssembler(6, TRACK_AUDIO)
    _feed(asm, [_group(1, 10.0, video=False), _group(2, 10.0 + AUDIO_SEG, video=False)])
    chunk = asm.flush()
    assert chunk is not None and set(chunk.tracks) == {TRACK_AUDIO}


def test_assembler_breaks_on_clock_wrap_but_keeps_unwrapped_time():
    asm = ChunkAssembler(60, TRACK_AUDIO)
    first = TS_WRAP_SECONDS - 0.5
    chunks = _feed(asm, [_group(1, first, duration=1.0), _group(2, first + 1.0 - TS_WRAP_SECONDS, duration=1.0)])
    assert [(c.first_sequence, c.last_sequence) for c in chunks] == [(1, 1)]
    rest = asm.flush()
    assert rest is not None
    # VOD time keeps counting past the wrap...
    assert rest.clock == pytest.approx(first + 1.0)
    # ...while the mux uses the raw clock that matches the TS timestamps.
    assert rest.tracks[TRACK_AUDIO].clock == pytest.approx(0.5)


def test_assembler_breaks_when_the_init_segment_changes():
    asm = ChunkAssembler(60, TRACK_AUDIO)
    g1 = SegmentGroup(1, {TRACK_AUDIO: TrackSegment(TRACK_AUDIO, 1, b"f1", CONTAINER_FMP4, 10.0, 1.0, init=b"init-a")})
    g2 = SegmentGroup(2, {TRACK_AUDIO: TrackSegment(TRACK_AUDIO, 2, b"f2", CONTAINER_FMP4, 11.0, 1.0, init=b"init-b")})
    chunks = _feed(asm, [g1, g2])
    assert len(chunks) == 1 and chunks[0].tracks[TRACK_AUDIO].init == b"init-a"
    rest = asm.flush()
    assert rest is not None and rest.tracks[TRACK_AUDIO].init == b"init-b"


def test_assembler_muxed_clock_track():
    asm = ChunkAssembler(2, TRACK_MUXED)
    g = [SegmentGroup(i, {TRACK_MUXED: TrackSegment(TRACK_MUXED, i, b"m", CONTAINER_MPEGTS, 30.0 + i, 1.0)}) for i in range(2)]
    chunks = _feed(asm, g)
    assert len(chunks) == 1 and chunks[0].clock == pytest.approx(30.0)


def test_assembler_flush_empty():
    assert ChunkAssembler(6, TRACK_AUDIO).flush() is None


# ----------------------------------------------------------------------
# Mux
# ----------------------------------------------------------------------


def test_build_mux_command_uses_copyts_and_places_adts_on_the_clock():
    video = ChunkTrack(CONTAINER_MPEGTS, 2001.2, None)
    audio = ChunkTrack(CONTAINER_ADTS, 2001.188567, None)
    cmd = build_mux_command([(TRACK_VIDEO, video, "v.ts"), (TRACK_AUDIO, audio, "a.aac")])
    assert "-copyts" in cmd
    assert cmd[cmd.index("-itsoffset") + 1] == "2001.188567"
    # -itsoffset applies to the audio input, which must follow it.
    assert cmd.index("-itsoffset") < cmd.index("a.aac") and cmd.index("-itsoffset") > cmd.index("v.ts")
    assert cmd[-5:] == ["-c", "copy", "-f", "mpegts", "pipe:1"]
    assert cmd[cmd.index("0:v:0?") - 1 : cmd.index("0:v:0?") + 1] == ["-map", "0:v:0?"]
    assert "1:a:0?" in cmd


def test_build_mux_command_ts_and_fmp4_keep_their_own_timestamps():
    muxed = ChunkTrack(CONTAINER_MPEGTS, 5.0, None)
    fmp4 = ChunkTrack(CONTAINER_FMP4, 5.0, b"init")
    assert "-itsoffset" not in build_mux_command([(TRACK_MUXED, muxed, "m.ts")])
    cmd = build_mux_command([(TRACK_AUDIO, fmp4, "a.mp4")])
    assert "-itsoffset" not in cmd and cmd[cmd.index("-i") - 1] == "mp4"


def test_mux_chunk_reports_ffmpeg_failure_and_cleans_up(tmp_path, mocker):
    from live_transcript_worker.youtube_timeline import Chunk

    chunk = Chunk(1, 1, 10.0, 1.0, {TRACK_AUDIO: ChunkTrack(CONTAINER_ADTS, 10.0, None, bytearray(b"junk"))})
    mocker.patch(
        "live_transcript_worker.youtube_timeline.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, b"", b"Invalid data found"),
    )
    with pytest.raises(MuxError, match="Invalid data found"):
        mux_chunk(chunk, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_mux_chunk_timeout(tmp_path, mocker):
    from live_transcript_worker.youtube_timeline import Chunk

    chunk = Chunk(1, 1, 10.0, 1.0, {TRACK_AUDIO: ChunkTrack(CONTAINER_ADTS, 10.0, None, bytearray(b"junk"))})
    mocker.patch("live_transcript_worker.youtube_timeline.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30))
    with pytest.raises(MuxError, match="timed out"):
        mux_chunk(chunk, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_mux_chunk_without_media():
    from live_transcript_worker.youtube_timeline import Chunk

    with pytest.raises(MuxError):
        mux_chunk(Chunk(1, 1, 0.0, 0.0, {}), "/nonexistent-but-unused")


# ----------------------------------------------------------------------
# Real ffmpeg: split video + raw audio stay on YouTube's clock
# ----------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _hls_like_segments(tmp_path, video_clock, audio_clock, count=3):
    """Real media cut into 1 s HLS-style segments: TS video whose PTS start at
    video_clock, and raw ADTS audio segments each headed by an ID3 timestamp."""
    video = tmp_path / "video.ts"
    audio = tmp_path / "audio.aac"
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=64x48:rate=10:duration={count}",
        "-c:v", "mpeg2video", "-g", "10", "-output_ts_offset", str(video_clock - 1.4), "-f", "mpegts", str(video),
    )  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={count}", "-c:a", "aac", "-f", "adts", str(audio))
    frames = []
    data = audio.read_bytes()
    pos = 0
    while pos + 7 <= len(data):
        length = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        frames.append(data[pos : pos + length])
        pos += length
    per_segment = 43
    segments = []
    clock = audio_clock
    for i in range(0, len(frames), per_segment):
        part = frames[i : i + per_segment]
        segments.append(id3_timestamp_tag(clock) + b"".join(part))
        clock += len(part) * 1024 / 44100
    return video.read_bytes(), segments


def _start_times(data: bytes) -> dict[str, float]:
    with av.open(io.BytesIO(data)) as c:
        out = {}
        for s in c.streams:
            first = next(p for p in c.demux(s) if p.pts is not None)
            out[s.type] = float(first.pts * first.time_base)
        return out


@needs_ffmpeg
def test_mux_keeps_split_tracks_on_the_source_clock(tmp_path):
    video_clock, audio_clock = 1000.0, 1000.25
    video, audio_segments = _hls_like_segments(tmp_path, video_clock, audio_clock)
    ts_info = scan_ts_pts(video)
    assert ts_info is not None and ts_info.video_pts is not None

    asm = ChunkAssembler(60, TRACK_AUDIO)
    for i, seg in enumerate(audio_segments):
        info = parse_adts(seg)
        clock = parse_id3_timestamp(seg)
        assert info is not None and clock is not None
        segments = {TRACK_AUDIO: TrackSegment(TRACK_AUDIO, i, seg, CONTAINER_ADTS, clock, info.duration)}
        if i == 0:
            segments[TRACK_VIDEO] = TrackSegment(TRACK_VIDEO, i, video, CONTAINER_MPEGTS, ts_info.video_pts, 1.0)
        asm.add(SegmentGroup(i, segments))
    chunk = asm.flush()
    assert chunk is not None and chunk.clock == pytest.approx(audio_clock, abs=1e-4)

    out = mux_chunk(chunk, str(tmp_path / "work"))
    starts = _start_times(out)
    # Both tracks keep their source clock (shifted together by the TS muxer), so
    # the source's A/V offset survives the mux exactly.
    assert starts["audio"] - starts["video"] == pytest.approx(audio_clock - ts_info.video_pts, abs=0.002)
    # Every audio frame of the concatenated, ID3-separated segments made it through.
    with av.open(io.BytesIO(out)) as c:
        n_audio = sum(1 for p in c.demux(c.streams.audio[0]) if p.size)
    assert n_audio == sum(parse_adts(s).frames for s in audio_segments)  # type: ignore[union-attr]


@needs_ffmpeg
def test_mux_audio_only_chunk(tmp_path):
    _, audio_segments = _hls_like_segments(tmp_path, 10.0, 20.0, count=2)
    asm = ChunkAssembler(60, TRACK_AUDIO)
    for i, seg in enumerate(audio_segments):
        asm.add(SegmentGroup(i, {TRACK_AUDIO: TrackSegment(TRACK_AUDIO, i, seg, CONTAINER_ADTS, parse_id3_timestamp(seg) or 0.0, 1.0)}))
    chunk = asm.flush()
    assert chunk is not None
    starts = _start_times(mux_chunk(chunk, str(tmp_path / "work")))
    assert set(starts) == {"audio"}


# ----------------------------------------------------------------------
# inspect_segment / fragmented MP4
# ----------------------------------------------------------------------

from live_transcript_worker.youtube_timeline import (  # noqa: E402
    NOMINAL_TOLERANCE_SECONDS,
    fmp4_timescale,
    inspect_segment,
    parse_fmp4,
)
from tests.timeline_fixtures import fmp4_fragment, fmp4_init  # noqa: E402


def test_inspect_segment_adts_uses_frame_exact_tolerance():
    seg = inspect_segment(TRACK_AUDIO, 7, audio_segment(100.0, frames=43), 1.0)
    assert seg is not None and seg.container == CONTAINER_ADTS
    assert seg.clock == pytest.approx(100.0, abs=1e-4)
    assert seg.duration == pytest.approx(audio_duration(43))
    assert seg.tolerance == pytest.approx(512 / 44100)


def test_inspect_segment_adts_without_timestamp_keeps_the_audio():
    seg = inspect_segment(TRACK_AUDIO, 7, b"".join(adts_frame() for _ in range(5)), 1.0)
    assert seg is not None and seg.clock is None and seg.duration == pytest.approx(audio_duration(5))


def test_inspect_segment_ts_tracks():
    video = inspect_segment(TRACK_VIDEO, 7, video_segment(100.01), 1.0)
    assert video is not None and video.clock == pytest.approx(100.01, abs=1e-4)
    assert video.duration == 1.0 and video.tolerance == NOMINAL_TOLERANCE_SECONDS
    # A TS segment without audio PES can't be the audio track.
    assert inspect_segment(TRACK_AUDIO, 7, video_segment(100.0), 1.0) is None
    muxed = inspect_segment(TRACK_MUXED, 7, ts_pes_packet(0x100, 0xE0, 50.0) + ts_pes_packet(0x101, 0xC0, 49.99), 2.0)
    assert muxed is not None and muxed.clock == pytest.approx(49.99, abs=1e-4)


def test_inspect_segment_rejects_unreadable():
    assert inspect_segment(TRACK_AUDIO, 1, b"<html>error</html>", 1.0) is None
    assert inspect_segment(TRACK_AUDIO, 1, id3_timestamp_tag(1.0), 1.0) is None  # tag only, no frames


def test_parse_fmp4_self_initializing_fragment():
    """YouTube's live fragments carry their own moov, then moof/mdat pairs."""
    data = fmp4_init(timescale=44100) + fmp4_fragment(decode_time=round(100.124444 * 44100), durations=[1024] * 43)
    info = parse_fmp4(data)
    assert info is not None
    assert info.timescale == 44100
    assert info.start == pytest.approx(100.124444, abs=1e-5)
    assert info.duration == pytest.approx(43 * 1024 / 44100)


def test_parse_fmp4_multiple_moofs_and_default_durations():
    data = (
        fmp4_init(timescale=90000, trex_default=3000)
        + fmp4_fragment(decode_time=900000, count=30)  # trex default
        + fmp4_fragment(decode_time=990000, count=30, default_duration=3000, version=0)  # tfhd default
    )
    info = parse_fmp4(data)
    assert info is not None and info.start == pytest.approx(10.0) and info.duration == pytest.approx(2.0)


def test_parse_fmp4_timescale_from_separate_init():
    fragment = fmp4_fragment(decode_time=48000 * 5, durations=[1024] * 10)
    assert parse_fmp4(fragment) is None
    assert fmp4_timescale(fmp4_init(timescale=48000)) == 48000
    info = parse_fmp4(fragment, timescale=48000)
    assert info is not None and info.start == pytest.approx(5.0)
    seg = inspect_segment(TRACK_AUDIO, 1, fragment, 1.0, init=fmp4_init(timescale=48000))
    assert seg is not None and seg.container == CONTAINER_FMP4 and seg.init is not None and seg.clock == pytest.approx(5.0)


def test_parse_fmp4_rejects_garbage():
    assert parse_fmp4(b"") is None
    assert parse_fmp4(b"\x00\x00\x00\x08moov") is None
    assert parse_fmp4(fmp4_init()) is None  # no fragment


# ----------------------------------------------------------------------
# Assembler: continuity, derived clocks, duplicates, wrap seeding
# ----------------------------------------------------------------------


def _adts(sequence, clock, frames=43):
    return TrackSegment(TRACK_AUDIO, sequence, b"a%d" % sequence, CONTAINER_ADTS, clock, audio_duration(frames), 512 / 44100)


def test_assembler_breaks_on_a_one_frame_audio_gap():
    """Raw ADTS is laid out back to back, so even one missing AAC frame inside a
    chunk would shift everything after it: that must end the chunk."""
    asm = ChunkAssembler(60, TRACK_AUDIO)
    frame = 1024 / 44100
    chunks = _feed(
        asm, [SegmentGroup(1, {TRACK_AUDIO: _adts(1, 10.0)}), SegmentGroup(2, {TRACK_AUDIO: _adts(2, 10.0 + AUDIO_SEG + frame)})]
    )
    assert len(chunks) == 1 and chunks[0].last_sequence == 1


def test_assembler_derives_a_missing_audio_timestamp_from_the_previous_segment():
    asm = ChunkAssembler(60, TRACK_AUDIO)
    _feed(asm, [SegmentGroup(1, {TRACK_AUDIO: _adts(1, 10.0)}), SegmentGroup(2, {TRACK_AUDIO: _adts(2, None)})])
    _feed(asm, [SegmentGroup(3, {TRACK_AUDIO: _adts(3, 10.0 + 2 * AUDIO_SEG)})])
    chunk = asm.flush()
    assert chunk is not None and (chunk.first_sequence, chunk.last_sequence) == (1, 3)
    assert asm.derived_clocks == 1


def test_assembler_starts_a_run_from_the_video_clock_when_audio_has_none():
    asm = ChunkAssembler(60, TRACK_AUDIO)
    video = TrackSegment(TRACK_VIDEO, 5, b"v", CONTAINER_MPEGTS, 20.004, 1.0)
    _feed(asm, [SegmentGroup(5, {TRACK_AUDIO: _adts(5, None), TRACK_VIDEO: video})])
    chunk = asm.flush()
    assert chunk is not None and chunk.clock == pytest.approx(20.004)


def test_assembler_drops_a_group_without_any_clock():
    gaps = []
    asm = ChunkAssembler(60, TRACK_AUDIO, on_gap=gaps.append)
    _feed(asm, [SegmentGroup(1, {TRACK_AUDIO: _adts(1, 10.0)})])
    chunks = _feed(asm, [SegmentGroup(3, {TRACK_AUDIO: _adts(3, None)})])  # not contiguous, no video
    assert [c.last_sequence for c in chunks] == [1]
    assert asm.flush() is None  # sequence 3 was dropped
    assert gaps and "no usable audio" in gaps[0]


def test_assembler_limits_derived_clocks_in_a_row():
    asm = ChunkAssembler(600, TRACK_AUDIO)
    chunks = _feed(asm, [SegmentGroup(0, {TRACK_AUDIO: _adts(0, 10.0)})])
    for s in range(1, ChunkAssembler.MAX_DERIVED_CLOCKS + 2):
        chunks += asm.add(SegmentGroup(s, {TRACK_AUDIO: _adts(s, None)}))
    assert len(chunks) == 1 and chunks[0].last_sequence == ChunkAssembler.MAX_DERIVED_CLOCKS


def test_assembler_drops_duplicate_and_old_sequences():
    asm = ChunkAssembler(60, TRACK_AUDIO)
    _feed(asm, [SegmentGroup(1, {TRACK_AUDIO: _adts(1, 10.0)}), SegmentGroup(2, {TRACK_AUDIO: _adts(2, 10.0 + AUDIO_SEG)})])
    assert _feed(asm, [SegmentGroup(2, {TRACK_AUDIO: _adts(2, 10.0 + AUDIO_SEG)}), SegmentGroup(1, {TRACK_AUDIO: _adts(1, 10.0)})]) == []
    assert asm.duplicates == 2
    assert asm.flush().tracks[TRACK_AUDIO].data == b"a1a2"  # type: ignore[union-attr]


def test_unwrapper_seeded_after_a_wrap():
    """A run that starts 27 h into a stream sees a small raw clock; the seed puts it past the wrap."""
    u = ClockUnwrapper(expected=27 * 3600)
    raw = 27 * 3600 - TS_WRAP_SECONDS - 5.0
    assert u.unwrap(raw) == pytest.approx(27 * 3600 - 5.0)


def test_unwrapper_seed_never_goes_negative_and_none_period():
    assert ClockUnwrapper(expected=10.0).unwrap(5000.0) == pytest.approx(5000.0)
    u = ClockUnwrapper(wrap_period=None, expected=1e9)
    assert u.unwrap(12.5) == 12.5 and u.to_raw(12.5) == 12.5


def test_assembler_chunk_clock_is_unwrapped_but_mux_clock_is_raw():
    asm = ChunkAssembler(60, TRACK_AUDIO, ClockUnwrapper(expected=27 * 3600))
    raw = 27 * 3600 - TS_WRAP_SECONDS
    _feed(asm, [SegmentGroup(1, {TRACK_AUDIO: _adts(1, raw)})])
    chunk = asm.flush()
    assert chunk is not None
    assert chunk.clock == pytest.approx(27 * 3600)
    assert chunk.tracks[TRACK_AUDIO].clock == pytest.approx(raw)


def test_build_mux_command_formats_tiny_offsets_without_exponent():
    cmd = build_mux_command([(TRACK_AUDIO, ChunkTrack(CONTAINER_ADTS, 1 / 90000, None), "a.aac")])
    assert cmd[cmd.index("-itsoffset") + 1] == "0.000011"


def test_build_mux_command_disables_ts_overflow_correction_for_ts_inputs():
    cmd = build_mux_command([(TRACK_VIDEO, ChunkTrack(CONTAINER_MPEGTS, 1.0, None), "v.ts")])
    i = cmd.index("-correct_ts_overflow")
    assert cmd[i + 1] == "0" and cmd.index("v.ts") > i


@needs_ffmpeg
def test_mux_keeps_tracks_in_sync_just_below_the_33_bit_wrap(tmp_path):
    video_clock = TS_WRAP_SECONDS - 22.0
    audio_clock = video_clock + 0.3
    video, audio_segments = _hls_like_segments(tmp_path, video_clock, audio_clock)
    ts_info = scan_ts_pts(video)
    assert ts_info is not None and ts_info.video_pts is not None
    asm = ChunkAssembler(60, TRACK_AUDIO, ClockUnwrapper(expected=video_clock))
    for i, seg in enumerate(audio_segments):
        segments = {TRACK_AUDIO: inspect_segment(TRACK_AUDIO, i, seg, 1.0)}
        if i == 0:
            segments[TRACK_VIDEO] = inspect_segment(TRACK_VIDEO, i, video, 1.0)
        asm.add(SegmentGroup(i, segments))  # type: ignore[arg-type]
    chunk = asm.flush()
    assert chunk is not None
    starts = _start_times(mux_chunk(chunk, str(tmp_path / "work")))
    assert starts["audio"] - starts["video"] == pytest.approx(audio_clock - ts_info.video_pts, abs=0.002)
    # The raw near-wrap timestamps are kept (read back here through PyAV's own
    # overflow correction, hence negative), not rebased to ~1.4 s by the mux.
    assert starts["video"] == pytest.approx(ts_info.video_pts + 1.4 - TS_WRAP_SECONDS, abs=0.01)


@needs_ffmpeg
def test_mux_keeps_fmp4_tracks_on_their_tfdt(tmp_path):
    """The path a SABR source would use: fragmented MP4 tracks whose tfdt is the stream clock."""
    from tests.timeline_fixtures import shift_tfdt

    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3", "-c:a", "aac", "-f", "mp4",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "1000000", str(tmp_path / "a.mp4"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=3", "-c:v", "mpeg4", "-g", "10", "-f", "mp4",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(tmp_path / "v.mp4"))  # fmt: skip
    audio = shift_tfdt((tmp_path / "a.mp4").read_bytes(), 500.25)
    video = shift_tfdt((tmp_path / "v.mp4").read_bytes(), 500.0)
    a = inspect_segment(TRACK_AUDIO, 1, audio, 1.0)
    v = inspect_segment(TRACK_VIDEO, 1, video, 1.0)
    assert a is not None and v is not None and a.clock == pytest.approx(500.25) and v.clock == pytest.approx(500.0)
    asm = ChunkAssembler(60, TRACK_AUDIO, ClockUnwrapper(None))
    asm.add(SegmentGroup(1, {TRACK_AUDIO: a, TRACK_VIDEO: v}))
    chunk = asm.flush()
    assert chunk is not None
    starts = _start_times(mux_chunk(chunk, str(tmp_path / "work")))
    assert starts["audio"] - starts["video"] == pytest.approx(0.25, abs=0.002)


def test_inspect_segment_muxed_without_audio_has_no_clock():
    """A muxed segment's clock is its audio's: transcription offsets count from the first audio sample."""
    assert inspect_segment(TRACK_MUXED, 7, ts_pes_packet(0x100, 0xE0, 50.0) + ts_filler_packet(0x100), 2.0) is None


def test_assembler_derives_a_missing_timestamp_right_after_a_chunk_closed():
    """With 5 s segments every segment closes a chunk: the run still continues,
    so a segment without its ID3 timestamp takes the exact expected clock."""
    asm = ChunkAssembler(6, TRACK_AUDIO)
    five = 5 * 44100 // 1024  # ~5 s of frames
    seg = audio_duration(five)

    def adts(sequence, clock):
        return TrackSegment(TRACK_AUDIO, sequence, b"a%d" % sequence, CONTAINER_ADTS, clock, seg, 512 / 44100)

    chunks = _feed(asm, [SegmentGroup(1, {TRACK_AUDIO: adts(1, 100.0)}), SegmentGroup(2, {TRACK_AUDIO: adts(2, None)})])
    assert [(c.first_sequence, c.clock) for c in chunks] == [(1, 100.0), (2, pytest.approx(100.0 + seg))]
    assert chunks[1].tracks[TRACK_AUDIO].clock == pytest.approx(100.0 + seg)
    assert asm.derived_clocks == 1


def test_mux_chunk_reports_file_errors_as_mux_errors(tmp_path, mocker):
    from live_transcript_worker.youtube_timeline import Chunk

    chunk = Chunk(1, 1, 10.0, 1.0, {TRACK_AUDIO: ChunkTrack(CONTAINER_ADTS, 10.0, None, bytearray(b"junk"))})
    mocker.patch("live_transcript_worker.youtube_timeline.tempfile.mkstemp", side_effect=OSError(28, "No space left on device"))
    with pytest.raises(MuxError, match="No space left"):
        mux_chunk(chunk, str(tmp_path))
