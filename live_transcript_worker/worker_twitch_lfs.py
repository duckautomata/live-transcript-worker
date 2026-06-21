import contextlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable

from live_transcript_worker.custom_types import Media, ProcessObject, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.worker_abstract import AbstractWorker

logger = logging.getLogger(__name__)


class TwitchLFSWorker(AbstractWorker):
    """
    Worker for Twitch streams using --live-from-start.

    yt-dlp pipes the stream to ffmpeg, which writes keyframe-aligned MPEG-TS
    segments to a directory. Each segment is independently decodable (starts at
    an IDR with SPS/PPS) and has accurate duration metadata — eliminating black
    frames and timestamp drift that arise from manual byte-level splitting.

    Timestamps are derived from info.start_time plus the accumulated duration of
    all previously emitted segments, measured by StreamHelper.get_duration on the
    actual segment bytes.
    """

    _SEGMENT_PREFIX = "chunk"
    _SEGMENT_EXT = ".ts"

    # Per-run outcome, reset at the top of start(). segments_produced lets
    # Worker._start_twitch tell a real capture from a fast --live-from-start
    # failure; _on_first_segment fires once so the caller can persist progress
    # only after the first segment is captured.
    segments_produced: int = 0
    _on_first_segment: Callable[[], None] | None = None

    def start(self, info: StreamInfoObject, on_first_segment: Callable[[], None] | None = None):
        logger.info(f"[{info.key}][TwitchLFSWorker] Starting")

        # The worker instance is reused across streams, so reset per-run state.
        self.segments_produced = 0
        self._on_first_segment = on_first_segment

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        segment_dir = os.path.join(project_root, "tmp", info.key, "lfs_segments")

        if os.path.exists(segment_dir):
            shutil.rmtree(segment_dir)
        os.makedirs(segment_dir)

        try:
            audio_start_time = float(info.start_time)
        except (ValueError, TypeError):
            audio_start_time = time.time()
            logger.warning(f"[{info.key}][TwitchLFSWorker] Invalid start_time, defaulting to system time.")

        # yt-dlp's stderr is written to a persistent per-key log (same pattern as
        # DASHWorker) so a failed --live-from-start is diagnosable after the fact,
        # instead of being discarded while the worker merely appears to produce nothing.
        ytdlp_log_path = os.path.join(os.path.dirname(segment_dir), "ytdlp.log")

        ytdlp_proc = self._create_ytdlp_process(info, ytdlp_log_path)
        if ytdlp_proc is None:
            return

        ffmpeg_proc = self._create_ffmpeg_process(info, segment_dir, ytdlp_proc.stdout)
        if ffmpeg_proc is None:
            ytdlp_proc.terminate()
            return

        try:
            self._monitor_segments(info, segment_dir, ytdlp_proc, ffmpeg_proc, audio_start_time)
        finally:
            # Sample yt-dlp's own exit code before we terminate it below: None here
            # means it was still running (we are stopping it), not that it failed.
            ytdlp_natural_exit = ytdlp_proc.poll()
            # Terminating yt-dlp closes the pipe, which causes ffmpeg to flush and exit cleanly.
            for proc in (ytdlp_proc, ffmpeg_proc):
                if proc.poll() is None:
                    proc.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
            self._log_ytdlp_outcome(info, ytdlp_natural_exit, ytdlp_log_path)
            with contextlib.suppress(Exception):
                shutil.rmtree(segment_dir)

    # ------------------------------------------------------------------
    # Segment monitor
    # ------------------------------------------------------------------

    def _monitor_segments(
        self,
        info: StreamInfoObject,
        segment_dir: str,
        ytdlp_proc: subprocess.Popen,
        ffmpeg_proc: subprocess.Popen,
        audio_start_time: float,
    ):
        next_seq = 0

        while not self.stop_event.is_set():
            seg_path = self._seg_path(segment_dir, next_seq)
            next_seg_path = self._seg_path(segment_dir, next_seq + 1)

            both_done = ytdlp_proc.poll() is not None and ffmpeg_proc.poll() is not None

            # A segment is safe to read when the next one has appeared (ffmpeg has
            # moved on) or when both upstream processes have exited (last segment).
            seg_ready = os.path.exists(seg_path) and (os.path.exists(next_seg_path) or both_done)

            if seg_ready:
                try:
                    with open(seg_path, "rb") as f:
                        data = f.read()
                except Exception as e:
                    logger.error(f"[{info.key}][TwitchLFSWorker] Failed to read segment {next_seq}: {e}")
                    next_seq += 1
                    continue
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(seg_path)

                duration = StreamHelper.get_precise_duration(data)
                if duration > 0 and data:
                    process_obj = ProcessObject(
                        raw=data,
                        audio_start_time=audio_start_time,
                        key=info.key,
                        media_type=info.media_type,
                        vod_accurate=True,
                    )
                    logger.debug(f"[{info.key}][TwitchLFSWorker] Queuing segment {next_seq}. Duration: {duration:.3f}s")
                    self.queue.put(process_obj)
                    audio_start_time += duration
                    self.segments_produced += 1
                    if self.segments_produced == 1 and self._on_first_segment is not None:
                        # Persist progress (the stream id) only once LFS has proven it
                        # can capture this stream — keeps a fast-failed attempt
                        # retryable while staying crash-safe mid-stream.
                        try:
                            self._on_first_segment()
                        except Exception as e:
                            logger.warning(f"[{info.key}][TwitchLFSWorker] on_first_segment callback failed: {e}")
                else:
                    logger.warning(f"[{info.key}][TwitchLFSWorker] Segment {next_seq} has no usable data, skipping.")

                next_seq += 1

                # If both processes are done and there are no more segments, we're finished.
                if both_done and not os.path.exists(self._seg_path(segment_dir, next_seq)):
                    logger.info(f"[{info.key}][TwitchLFSWorker] Stream ended after {next_seq} segments.")
                    break
            else:
                if both_done and not os.path.exists(seg_path):
                    logger.info(f"[{info.key}][TwitchLFSWorker] Both processes exited, no more segments.")
                    break
                time.sleep(0.5)

            # Check if worker is too far behind live
            gap = time.time() - audio_start_time
            if gap > self.stale_lfs_gap_seconds:
                logger.warning(
                    f"[{info.key}][TwitchLFSWorker] Worker is {gap / 60:.1f} minutes behind live "
                    f"(threshold: {self.stale_lfs_gap_seconds / 60:.1f} min). Switching to LiveSegmentWorker."
                )
                self.is_slow = True
                break

    # ------------------------------------------------------------------
    # Process creation
    # ------------------------------------------------------------------

    def _create_ytdlp_process(self, info: StreamInfoObject, log_path: str) -> subprocess.Popen | None:
        try:
            fmt_selector = "best" if info.media_type == Media.VIDEO else "ba/best"
            cmd = [
                self.ytdlp_path,
                "--quiet",
                "--live-from-start",
                *StreamHelper.ytdlp_auth_args(info.url, purpose="download"),
                "-f",
                fmt_selector,
                "-o",
                "-",
                info.url,
            ]
            # Mirror DASHWorker: send yt-dlp's stderr to a persistent per-key log file
            # (stdout is the media pipe to ffmpeg, so it can't be redirected). Dropping
            # --no-warnings lets the reason for a failed --live-from-start reach the log.
            # The parent closes its copy of the fd after Popen; the child keeps writing
            # to its own inherited fd.
            with open(log_path, "a") as log_file:
                log_file.write(f"\n--- yt-dlp (TwitchLFS) started at {time.strftime('%Y-%m-%d %H:%M:%S')} (stream: {info.stream_id}) ---\n")
                log_file.flush()
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log_file)
            logger.debug(f"[{info.key}][TwitchLFSWorker] yt-dlp started (format: {fmt_selector}). Logging to {log_path}")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][TwitchLFSWorker] Failed to start yt-dlp: {e}")
            return None

    def _create_ffmpeg_process(
        self,
        info: StreamInfoObject,
        segment_dir: str,
        stdin,
    ) -> subprocess.Popen | None:
        try:
            output_pattern = os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}%06d{self._SEGMENT_EXT}")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                "pipe:0",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-f",
                "segment",
                "-segment_time",
                str(self.buffer_size_seconds),
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
                output_pattern,
            ]
            proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.debug(f"[{info.key}][TwitchLFSWorker] ffmpeg segmenter started (target segment: {self.buffer_size_seconds}s)")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][TwitchLFSWorker] Failed to start ffmpeg: {e}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log_ytdlp_outcome(self, info: StreamInfoObject, natural_exit: int | None, log_path: str) -> None:
        """Flag a failed run in the app log and point at the per-key yt-dlp log for
        details. Without this, a fast --live-from-start failure (e.g. a Twitch stream
        with VODs disabled) is invisible apart from the empty output.

        natural_exit is yt-dlp's exit code sampled before we terminated it; None means
        it was still running when the worker stopped (a deliberate stop or a
        fall-behind switch), which is not a failure.
        """
        if self.stop_event.is_set():
            return  # intentional shutdown/restart, not a failure
        exited_with_error = natural_exit is not None and natural_exit != 0
        if not exited_with_error and self.segments_produced > 0:
            return  # healthy run
        logger.warning(
            f"[{info.key}][TwitchLFSWorker] yt-dlp exited (code={natural_exit}) after "
            f"{self.segments_produced} segment(s); see {log_path} for details."
        )

    def _seg_path(self, segment_dir: str, seq: int) -> str:
        return os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}{seq:06d}{self._SEGMENT_EXT}")
