import contextlib
import dataclasses
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TextIO

from live_transcript_worker.custom_types import ProcessObject, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.worker_abstract import AbstractWorker
from live_transcript_worker.youtube_timeline import Chunk, ChunkAssembler, ClockUnwrapper, MuxError, mux_chunk
from live_transcript_worker.youtube_timeline_source import (
    ResolvedStream,
    ResolveError,
    ResolveStopped,
    SegmentSource,
    SourceContext,
    SourceError,
    SourceExpiredError,
    SourceResetError,
    StreamNotLiveError,
    StreamUnavailableError,
    UnsupportedSourceError,
    create_segment_source,
    resolve_stream,
    watch_url,
)

logger = logging.getLogger(__name__)


class _EndRun(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _LockedLog:
    """The per-key run log as a file-like object several threads can write to
    (a URL refresh runs yt-dlp off the worker thread)."""

    def __init__(self, worker: "YouTubeTimelineWorker"):
        self._worker = worker

    def write(self, text: str) -> None:
        with self._worker._log_lock:
            log_file = self._worker._log_file
            if log_file is not None:
                with contextlib.suppress(OSError, ValueError):
                    log_file.write(text)

    def flush(self) -> None:
        with self._worker._log_lock:
            log_file = self._worker._log_file
            if log_file is not None:
                with contextlib.suppress(OSError, ValueError):
                    log_file.flush()


@dataclass
class _Outcome:
    kind: str  # "end", "reconnect", "swap" (fresh URLs resolved, keep going) or "reset" (start over at the live edge)
    reason: str
    resolved: ResolvedStream | None = None


class YouTubeTimelineWorker(AbstractWorker):
    """
    VOD-accurate YouTube worker that stays at the live edge.

    YouTube stamps every live segment with its media clock (TS PTS, or the ID3
    timestamp of the raw ADTS audio rendition), and that clock is the VOD
    timeline. This worker fetches the segments itself, pairs video and audio by
    sequence number, and stamps each chunk with
    `start_time + clock of its first audio sample + the source's VOD offset`,
    so every line is VOD-accurate without downloading the stream from the start
    (no DASH, no --live-from-start, works with DVR disabled). Losing segments
    loses only those lines: later ones still carry their own clock.

    Video and audio are muxed per chunk with ffmpeg -copyts, so both keep the
    clock and stay in sync (see youtube_timeline.build_mux_command).

    Transport lives in youtube_timeline_source: HLS today, SABR to be added as
    another SegmentSource.

    Health: the connection (yt-dlp resolution + HTTP sessions) is rebuilt when
    the URLs are rejected, the playlist stops advancing, segments keep arriving
    with nothing usable, or the video track stays missing; URLs are refreshed
    before they expire without dropping the current connection. yt-dlp runs are
    paced and back off while reconnects bring nothing. The run only ends when
    yt-dlp says the stream is no longer live (or can't be watched), when the
    playlist ends, or on stop, so the watcher doesn't flip the stream offline
    and back. A run resumes where the previous run of the same stream stopped
    (tmp/{key}/youtube_timeline_state.json) when that is within
    stale_threshold.lfs_gap_seconds of the live edge.
    Diagnostics: tmp/{key}/youtube_timeline.log.
    """

    _NAME = "YouTubeTimelineWorker"
    _LOG_FILENAME = "youtube_timeline.log"
    _STATE_FILENAME = "youtube_timeline_state.json"
    _WORK_SUBDIR = "youtube_timeline"

    # How long one next_group() call may wait; keeps the checks below responsive.
    _POLL_TIMEOUT_SECONDS = 2.0
    # Refresh the URLs this long before they expire, keeping the current connection until that succeeds...
    _REFRESH_BEFORE_EXPIRY_SECONDS = 20 * 60
    _PROACTIVE_RETRY_SECONDS = 60.0
    # ...and reconnect regardless this close to expiry.
    _HARD_REFRESH_BEFORE_EXPIRY_SECONDS = 60.0
    # An expiry missing or this close after resolving isn't trusted: rejected URLs (403) take over.
    _MIN_EXPIRY_HEADROOM_SECONDS = 15 * 60
    # No new segment on a connection for this long: rebuild it (frozen playlist, ended stream).
    _IDLE_RECONNECT_SECONDS = 30.0
    # yt-dlp never runs more often than this...
    _MIN_RESOLVE_INTERVAL_SECONDS = 15.0
    # ...and backs off like this while it fails or reconnects bring nothing usable.
    _BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0, 300.0)
    # yt-dlp offered nothing readable (e.g. SABR-only): ask again this rarely.
    _UNSUPPORTED_RETRY_SECONDS = 600.0
    # A secondary track (video) missing this many sequences in a row triggers a reconnect, at most this often.
    _TRACK_RECONNECT_AFTER = 30
    _TRACK_RECONNECT_INTERVAL_SECONDS = 600.0
    _MAX_SOURCE_ERRORS = 3
    # Smallest leftover worth transcribing when a run ends.
    _MIN_FLUSH_SECONDS = 0.5
    # Where start time + clock may put a chunk relative to now and still count as anchored to the
    # stream's start: up to this much later (start time recorded after the clock started, drift
    # over days)...
    _CLOCK_AHEAD_TOLERANCE_SECONDS = 900.0
    # ...or this much earlier on top of the catch-up window (delivery delay).
    _CLOCK_BEHIND_TOLERANCE_SECONDS = 300.0
    # Used only when the stream's start time is unknown: delay between a frame and its arrival.
    _ASSUMED_DELIVERY_SECONDS = 2.0
    # Repeated warnings of one kind are logged at most this often.
    _WARN_INTERVAL_SECONDS = 300.0
    # The run log is trimmed back to its size budget at most this often during a run.
    _TRIM_INTERVAL_SECONDS = 300.0

    # Time sources and the tmp root (overridable in tests).
    _monotonic = staticmethod(time.monotonic)
    _wallclock = staticmethod(time.time)
    _TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp")

    def __init__(self, key, queue, stop_event):
        super().__init__(key, queue, stop_event)
        # yt-dlp pacing survives across runs of the same stream (the watcher may restart a run).
        self._pacing_stream_id: str | None = None
        self._next_resolve_at = 0.0
        self._backoff_step = 0
        # URL refreshes run yt-dlp here, off the worker thread, so segments keep flowing meanwhile.
        self._refresh_pool: ThreadPoolExecutor | None = None
        self._log_lock = threading.Lock()
        self._locked_log = _LockedLog(self)
        self._reset_run()

    def _reset_run(self) -> None:
        self.chunks_produced = 0
        self.chunks_failed = 0
        self.chunks_not_vod_accurate = 0
        self.gaps = 0
        self.reconnects = 0
        self.segments_missing = 0
        self.segments_skipped = 0
        self._log_file: TextIO | None = None
        self._log_path = ""
        self._work_dir = ""
        self._state_path = ""
        self._start_time: float | None = None
        self._start_time_estimated = False
        self._info: StreamInfoObject | None = None
        self._vod_offset = 0.0
        self._warned_at: dict[str, float] = {}
        self._last_track_reconnect = -1e9
        self._unsupported_reason: str | None = None
        self._last_trim = -1e9

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][{self._NAME}] Starting")
        self._reset_run()
        self.is_slow = False
        self._info = info
        if self._pacing_stream_id != info.stream_id:
            self._pacing_stream_id = info.stream_id
            self._next_resolve_at = 0.0
            self._backoff_step = 0

        key_dir = os.path.join(self._TMP_ROOT, info.key)
        self._work_dir = os.path.join(key_dir, self._WORK_SUBDIR)
        self._log_path = os.path.join(key_dir, self._LOG_FILENAME)
        self._state_path = os.path.join(key_dir, self._STATE_FILENAME)
        shutil.rmtree(self._work_dir, ignore_errors=True)
        try:
            self._log_file = StreamHelper.open_process_log(self._log_path, self._NAME, info.stream_id)
        except OSError as e:
            logger.warning(f"[{info.key}][{self._NAME}] Cannot open {self._log_path} ({e}); diagnostics will only go to the app log.")

        self._start_time = self._parse_start_time(info.start_time)
        if self._start_time is None:
            self._event(f"start time {info.start_time!r} is not usable; it will be estimated from the first chunk", logging.WARNING)

        ctx = SourceContext(
            key=info.key,
            watch_url=watch_url(info.url, info.stream_id),
            ytdlp_path=self.ytdlp_path,
            media_type=info.media_type,
            stop_event=self.stop_event,
            log=self._event,
            log_file=self._locked_log,
            max_lag_seconds=float(self.stale_lfs_gap_seconds),
        )
        resume: int | None = None
        assembler: ChunkAssembler | None = None
        source: SegmentSource | None = None
        pending: ResolvedStream | None = None
        end_reason = "stopped"
        try:
            resume = self._load_state(info.stream_id)
            while not self.stop_event.is_set():
                resolved = pending if pending is not None else self._resolve(info, ctx)
                pending = None
                try:
                    source = create_segment_source(resolved, resume, ctx)
                except UnsupportedSourceError as e:
                    self._unsupported(str(e))
                    continue
                if (
                    assembler is None
                    or assembler.clock_track != source.clock_track
                    or assembler.unwrapper.wrap_period != source.wrap_period
                ):
                    if assembler is not None:
                        self._flush(info, assembler)
                    assembler = self._new_assembler(source)
                self._vod_offset = source.vod_offset

                chunks_before = self.chunks_produced
                current = source.resolved
                try:
                    outcome = self._consume(info, source, assembler)
                finally:
                    resume = source.checkpoint
                    self.segments_missing += source.segments_missing
                    self.segments_skipped += source.segments_skipped
                    source.close()
                    source = None

                if outcome.kind == "end":
                    end_reason = outcome.reason
                    break
                if outcome.kind == "swap":
                    # Same position, same clock: continue seamlessly on the new URLs.
                    self._event(outcome.reason)
                    pending = outcome.resolved
                    continue
                if outcome.kind == "reset":
                    # A new timeline: new clock state, and the new numbering read from its
                    # start on the same URLs (no yt-dlp run). The source clamps sequence 0 to
                    # the catch-up window or the oldest segment still available.
                    self._event(f"the stream's numbering restarted ({outcome.reason}); following the new one", logging.WARNING)
                    self._flush(info, assembler)
                    assembler = None
                    resume = 0
                    pending = current
                    self.reconnects += 1
                    continue
                self.reconnects += 1
                self._pace_after_connection(productive=self.chunks_produced > chunks_before)
                self._event(f"reconnecting: {outcome.reason}")
        except _EndRun as e:
            end_reason = e.reason
        except Exception as e:
            # Never let an unexpected error kill the watcher thread.
            logger.exception(f"[{info.key}][{self._NAME}] Unexpected error: {e}")
            end_reason = f"unexpected error: {e}"
        finally:
            if source is not None:
                with contextlib.suppress(Exception):
                    source.close()
            if assembler is not None:
                try:
                    self._flush(info, assembler)
                except Exception:
                    logger.exception(f"[{info.key}][{self._NAME}] Could not flush the last chunk")
            try:
                self._finish(info, end_reason)
            except Exception:
                logger.exception(f"[{info.key}][{self._NAME}] Could not finish the run cleanly")

    def _consume(self, info: StreamInfoObject, source: SegmentSource, assembler: ChunkAssembler) -> _Outcome:
        """Feeds one connection's segments to the assembler until it should be
        rebuilt, swapped for fresh URLs, reset, or the run should end."""
        start = self._monotonic()
        last_segment = start
        last_chunk = start
        next_refresh_try = 0.0
        refresh: Future | None = None
        errors = 0
        while not self.stop_event.is_set():
            expires_at = source.expires_at
            if expires_at is not None:
                remaining = expires_at - self._wallclock()
                if remaining < self._HARD_REFRESH_BEFORE_EXPIRY_SECONDS:
                    return _Outcome("reconnect", "the stream URLs are about to expire")
                if refresh is not None and refresh.done():
                    refreshed = self._refresh_result(refresh)
                    refresh = None
                    if refreshed is not None:
                        return _Outcome("swap", "refreshed the stream URLs before they expire", refreshed)
                now = self._monotonic()
                if (
                    refresh is None
                    and remaining < self._REFRESH_BEFORE_EXPIRY_SECONDS
                    and now >= max(next_refresh_try, self._next_resolve_at)
                ):
                    # Resolved in the background: the current URLs keep working meanwhile.
                    next_refresh_try = now + self._PROACTIVE_RETRY_SECONDS
                    self._next_resolve_at = now + self._MIN_RESOLVE_INTERVAL_SECONDS
                    refresh = self._refresh_executor().submit(self._resolve_fresh, info, source.ctx.watch_url)

            try:
                group = source.next_group(self._POLL_TIMEOUT_SECONDS)
            except SourceResetError as e:
                return _Outcome("reset", str(e))
            except SourceExpiredError as e:
                return _Outcome("reconnect", str(e))
            except SourceError as e:
                errors += 1
                self._event(f"source error {errors}/{self._MAX_SOURCE_ERRORS}: {e}", logging.WARNING)
                if errors >= self._MAX_SOURCE_ERRORS:
                    return _Outcome("reconnect", f"repeated source errors ({e})")
                self._sleep(min(10.0, 2.0**errors))
                continue
            errors = 0

            if source.ended:
                return _Outcome("end", "the stream ended")

            now = self._monotonic()
            if group is None:
                if now - last_segment > self._IDLE_RECONNECT_SECONDS:
                    return _Outcome("reconnect", f"no new segment in {now - last_segment:.0f}s")
                continue

            last_segment = now
            for chunk in assembler.add(group):
                if self._emit(info, chunk):
                    last_chunk = self._monotonic()

            if now - last_chunk > self.stale_unusable_seconds:
                return _Outcome("reconnect", f"nothing usable in {now - last_chunk:.0f}s")
            for name, missing in source.consecutive_missing.items():
                if name == source.clock_track or missing < self._TRACK_RECONNECT_AFTER:
                    continue
                if now - self._last_track_reconnect >= self._TRACK_RECONNECT_INTERVAL_SECONDS:
                    self._last_track_reconnect = now
                    return _Outcome("reconnect", f"no {name} for {missing} segments")
        return _Outcome("end", "stopped")

    # ------------------------------------------------------------------
    # Resolution (paced)
    # ------------------------------------------------------------------

    def _resolve(self, info: StreamInfoObject, ctx: SourceContext) -> ResolvedStream:
        """Resolves the stream's formats, retrying with backoff while the stream
        is live. Raises _EndRun when it isn't (or on stop)."""
        while True:
            if self.stop_event.is_set():
                raise _EndRun("stopped")
            wait = self._next_resolve_at - self._monotonic()
            if wait > 0:
                if wait > self._MIN_RESOLVE_INTERVAL_SECONDS:
                    self._event(f"waiting {wait:.0f}s before asking yt-dlp again")
                self._sleep(wait)
                continue
            self._next_resolve_at = self._monotonic() + self._MIN_RESOLVE_INTERVAL_SECONDS
            if self._log_path:
                StreamHelper.trim_log_file(self._log_path)
            try:
                resolved = resolve_stream(self.ytdlp_path, ctx.watch_url, info.media_type, self.stop_event, self._locked_log)
            except ResolveStopped as e:
                raise _EndRun("stopped") from e
            except StreamNotLiveError as e:
                self._event(f"the stream is not live: {e}")
                raise _EndRun("the stream is not live") from e
            except StreamUnavailableError as e:
                self._event(f"the stream is not available: {e}", logging.WARNING)
                raise _EndRun(f"the stream is not available ({e})") from e
            except UnsupportedSourceError as e:
                self._unsupported(str(e))
                continue
            except ResolveError as e:
                self._event(f"yt-dlp failed: {e}", logging.WARNING)
                self._backoff()
                continue
            self._unsupported_reason = None
            resolved = self._checked_expiry(resolved)
            formats = ", ".join(f"{t.track}={t.format_id}" for t in resolved.tracks)
            expiry = f", URLs valid for {(resolved.expires_at - self._wallclock()) / 3600:.1f}h" if resolved.expires_at else ""
            self._event(f"resolved {formats}{expiry}")
            return resolved

    def _refresh_executor(self) -> ThreadPoolExecutor:
        if self._refresh_pool is None:
            self._refresh_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"timeline-{self.key}-refresh")
        return self._refresh_pool

    def _resolve_fresh(self, info: StreamInfoObject, url: str) -> ResolvedStream:
        """Runs on the refresh thread."""
        return resolve_stream(self.ytdlp_path, url, info.media_type, self.stop_event, self._locked_log)

    def _refresh_result(self, future: Future) -> ResolvedStream | None:
        """The outcome of a background URL refresh; None keeps the current URLs."""
        try:
            resolved = future.result()
        except ResolveStopped:
            return None
        except ResolveError as e:
            # Keep using the current URLs; a stream that really ended stops advancing and is caught there.
            self._event(f"refreshing the URLs failed ({e}); keeping the current ones for now", logging.WARNING)
            return None
        except Exception as e:
            logger.exception(f"[{self.key}][{self._NAME}] URL refresh failed: {e}")
            return None
        return self._checked_expiry(resolved)

    def _checked_expiry(self, resolved: ResolvedStream) -> ResolvedStream:
        if resolved.expires_at is None:
            return resolved
        headroom = resolved.expires_at - self._wallclock()
        if headroom < self._MIN_EXPIRY_HEADROOM_SECONDS:
            self._event(
                f"the URLs claim to expire in {headroom:.0f}s; ignoring that and relying on rejected requests instead", logging.WARNING
            )
            return dataclasses.replace(resolved, expires_at=None)
        return resolved

    def _backoff(self) -> None:
        delay = self._BACKOFF_SECONDS[min(self._backoff_step, len(self._BACKOFF_SECONDS) - 1)]
        self._backoff_step += 1
        self._next_resolve_at = max(self._next_resolve_at, self._monotonic() + delay)

    def _pace_after_connection(self, productive: bool) -> None:
        if productive:
            self._backoff_step = 0
        else:
            self._backoff()

    def _unsupported(self, reason: str) -> None:
        if reason != self._unsupported_reason:
            self._event(f"cannot read this stream: {reason}. Retrying every {self._UNSUPPORTED_RETRY_SECONDS / 60:.0f} min.", logging.ERROR)
            self._unsupported_reason = reason
        self._next_resolve_at = max(self._next_resolve_at, self._monotonic() + self._UNSUPPORTED_RETRY_SECONDS)

    # ------------------------------------------------------------------
    # Chunks
    # ------------------------------------------------------------------

    def _new_assembler(self, source: SegmentSource) -> ChunkAssembler:
        # Seed the clock's wrap count from the time since the stream started, so
        # a run that begins after the 33-bit clock wrapped lands on the right side.
        expected = None if self._start_time is None else self._wallclock() - self._start_time
        unwrapper = ClockUnwrapper(source.wrap_period, expected)
        return ChunkAssembler(self.buffer_size_seconds, source.clock_track, unwrapper, on_gap=self._on_gap)

    def _emit(self, info: StreamInfoObject, chunk: Chunk) -> bool:
        """Muxes a chunk and queues it. False if it produced nothing usable."""
        try:
            raw = mux_chunk(chunk, self._work_dir)
        except MuxError as e:
            self.chunks_failed += 1
            self._warn("mux", f"mux failed for sequences {chunk.first_sequence}-{chunk.last_sequence}: {e}")
            return False
        except Exception as e:
            self.chunks_failed += 1
            logger.exception(f"[{info.key}][{self._NAME}] Mux failed unexpectedly: {e}")
            return False
        duration = StreamHelper.get_precise_duration(raw)
        if duration <= 0:
            self.chunks_failed += 1
            self._warn("decode", f"sequences {chunk.first_sequence}-{chunk.last_sequence} have no decodable audio")
            return False

        now = self._wallclock()
        vod_accurate = self._clock_trusted(chunk, now)
        if vod_accurate:
            audio_start_time = self._stream_start_time(chunk, now) + chunk.clock + self._vod_offset
        else:
            # The clock contradicts the wall clock: stamp by arrival, like LiveSegmentWorker.
            audio_start_time = now - duration - self.live_latency_seconds
            self.chunks_not_vod_accurate += 1

        if self.chunks_produced == 0:
            logger.info(
                f"[{info.key}][{self._NAME}] First chunk ready: {StreamHelper.describe_codecs(raw)}, {duration:.2f}s, "
                f"stream clock {chunk.clock:.3f}s"
            )
        process_obj = ProcessObject(
            raw=raw,
            audio_start_time=audio_start_time,
            key=info.key,
            media_type=info.media_type,
            vod_accurate=vod_accurate,
        )
        logger.debug(
            f"[{info.key}][{self._NAME}] Queuing sequences {chunk.first_sequence}-{chunk.last_sequence}. "
            f"Clock: {chunk.clock:.3f}s, duration: {duration:.3f}s"
        )
        self.queue.put(process_obj)
        self.chunks_produced += 1
        self._save_state(info.stream_id, chunk.last_sequence + 1)
        return True

    def _flush(self, info: StreamInfoObject, assembler: ChunkAssembler) -> None:
        chunk = assembler.flush()
        if chunk is not None and chunk.duration >= self._MIN_FLUSH_SECONDS:
            self._emit(info, chunk)

    def _on_gap(self, reason: str) -> None:
        self.gaps += 1
        self._event(f"gap: {reason}")

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_start_time(value) -> float | None:
        try:
            start = float(value)
        except (TypeError, ValueError):
            return None
        return start if start > 0 else None

    def _stream_start_time(self, chunk: Chunk, now: float) -> float:
        """The epoch of clock zero as the server sees it: the stream's start time
        (it subtracts that to get a line's VOD position). Estimated once, from
        this chunk's arrival, when the start time is unknown."""
        if self._start_time is None:
            self._start_time = now - (chunk.clock + chunk.duration) - self._ASSUMED_DELIVERY_SECONDS
            self._start_time_estimated = True
            self._event(f"estimated the stream start as {self._start_time:.0f}", logging.WARNING)
        return self._start_time

    def _clock_trusted(self, chunk: Chunk, now: float) -> bool:
        """Whether the stream's clock counts from the stream's start, i.e. start
        time + clock lands near now (minus delivery delay and any backlog being
        caught up). YouTube restarts a stream's timeline after some breaks (seen
        on 24/7 streams: sequence numbers and clock reset days in); its clock then
        says nothing about the VOD position."""
        if self._start_time is None or self._start_time_estimated:
            return True  # the start time is derived from the clock itself
        offset = self._start_time + chunk.clock + chunk.duration - now
        if -(self.stale_lfs_gap_seconds + self._CLOCK_BEHIND_TOLERANCE_SECONDS) <= offset <= self._CLOCK_AHEAD_TOLERANCE_SECONDS:
            return True
        self._warn(
            "clock",
            f"the stream clock puts this chunk {offset:+.0f}s from now, so it doesn't count from the stream's start "
            "(the stream's timeline restarted?); lines are stamped by arrival time and not marked VOD-accurate",
        )
        return False

    # ------------------------------------------------------------------
    # Resume state
    # ------------------------------------------------------------------

    def _load_state(self, stream_id: str) -> int | None:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        sequence = data.get("next_sequence")
        if data.get("stream_id") != stream_id or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return None
        self._event(f"resuming this stream from sequence {sequence}")
        return sequence

    def _save_state(self, stream_id: str, next_sequence: int) -> None:
        if not self._state_path:
            return
        try:
            directory = os.path.dirname(self._state_path)
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"stream_id": stream_id, "next_sequence": next_sequence}, f)
                os.replace(tmp_path, self._state_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except OSError as e:
            self._warn("state", f"cannot save the resume point: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _event(self, message: str, level: int = logging.INFO) -> None:
        """Logs to the app log and the per-key run log."""
        key = self._info.key if self._info else self.key
        logger.log(level, f"[{key}][{self._NAME}] {message}")
        with self._log_lock:
            if self._log_file is None:
                return
            now = self._monotonic()
            if self._log_path and now - self._last_trim >= self._TRIM_INTERVAL_SECONDS:
                # Long runs never pass through the trims at run start and end.
                self._last_trim = now
                StreamHelper.trim_log_file(self._log_path)
            with contextlib.suppress(OSError, ValueError):
                self._log_file.write(f"{time.strftime('%H:%M:%S')} {message}\n")
                self._log_file.flush()

    def _warn(self, kind: str, message: str) -> None:
        """A warning that may repeat every chunk: logged at most every few minutes per kind."""
        now = self._monotonic()
        if now - self._warned_at.get(kind, -1e9) >= self._WARN_INTERVAL_SECONDS:
            self._warned_at[kind] = now
            self._event(message, logging.WARNING)

    def _sleep(self, seconds: float) -> None:
        end = self._monotonic() + seconds
        while not self.stop_event.is_set():
            remaining = end - self._monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def _finish(self, info: StreamInfoObject, reason: str) -> None:
        summary = (
            f"chunks={self.chunks_produced}, failed={self.chunks_failed}, not VOD-accurate={self.chunks_not_vod_accurate}, "
            f"gaps={self.gaps}, missing segments={self.segments_missing}, skipped segments={self.segments_skipped}, "
            f"reconnects={self.reconnects}"
        )
        if self.stop_event.is_set():
            logger.info(f"[{info.key}][{self._NAME}] Stopped on request ({summary}).")
        elif self.chunks_produced == 0:
            logger.warning(f"[{info.key}][{self._NAME}] Run produced no chunks: {reason} ({summary}).")
        else:
            logger.info(f"[{info.key}][{self._NAME}] Run finished: {reason} ({summary}).")
        with self._log_lock:
            if self._log_file is not None:
                if self._log_path:
                    StreamHelper.trim_log_file(self._log_path)
                with contextlib.suppress(OSError, ValueError):
                    self._log_file.write(f"--- {self._NAME} stopped at {time.strftime('%Y-%m-%d %H:%M:%S')}: {reason} ({summary}) ---\n")
                    self._log_file.close()
                self._log_file = None
        shutil.rmtree(self._work_dir, ignore_errors=True)
