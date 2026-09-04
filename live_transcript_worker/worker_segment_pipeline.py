import contextlib
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
from typing import TextIO

from live_transcript_worker.custom_types import ProcessObject, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.worker_abstract import AbstractWorker

logger = logging.getLogger(__name__)


class SegmentPipelineWorker(AbstractWorker):
    """
    Shared pipeline for the workers that stream through ffmpeg's segmenter:

        yt-dlp -o - URL  |  ffmpeg -i pipe:0 -c copy -f segment -segment_format mpegts

    ffmpeg writes keyframe-aligned MPEG-TS segments of buffer_size_seconds to a
    directory; the monitor loop reads each finished segment, measures its real
    duration, timestamps it and queues it for transcription.

    Subclasses configure the run (name, directories, extra yt-dlp arguments,
    whether timestamps are VOD-accurate) and decide how each segment is
    timestamped via _timestamp_segment(); the optional _begin_run(),
    _after_segment() and _poll_hook() hooks carry per-run state.

    What the base class guarantees for every subclass:
    - Format selection never fails on an unknown codec: no [codec] filters, the
      H.264/AAC preference is a yt-dlp sort order (StreamHelper.FORMAT_SORT).
    - Diagnostics: the stderr of both yt-dlp and ffmpeg is appended to
      tmp/{key}/{_LOG_FILENAME}, one stamped header per run, together with the
      exact command lines and a footer carrying the exit codes, so a run that
      dies before its first segment is explainable after the fact.
    - Process hygiene: the parent drops its copy of the pipe once ffmpeg owns it
      (a dead ffmpeg surfaces as EPIPE in yt-dlp instead of a hang), a dead
      ffmpeg or a stalled yt-dlp is detected and stopped, yt-dlp is stopped
      gracefully (SIGINT) in its own process group so its internal ffmpeg can
      never be orphaned, and exit codes are reported.
    """

    _SEGMENT_PREFIX = "chunk"
    _SEGMENT_EXT = ".ts"

    # --- subclass configuration -------------------------------------------
    _NAME = "SegmentPipelineWorker"  # tag used in log lines
    _SEGMENT_SUBDIR = "segments"  # under tmp/{key}/
    _LOG_FILENAME = "segment_pipeline.log"  # under tmp/{key}/
    _YTDLP_EXTRA_ARGS: tuple[str, ...] = ()  # e.g. ("--live-from-start",)
    _VOD_ACCURATE = False  # ProcessObject.vod_accurate for queued segments

    # yt-dlp downloads live HLS through its own ffmpeg. These args keep that
    # ffmpeg's warnings and errors (the actual reason a download dies: HTTP 403,
    # playlist gone, ...) in the log while dropping its per-second progress line.
    # yt-dlp appends them after its own options, so they win.
    _FFMPEG_DOWNLOADER_ARGS = "ffmpeg:-loglevel warning -nostats"

    # How long ffmpeg gets to flush and exit after yt-dlp has gone away.
    _DRAIN_TIMEOUT_SECONDS = 30

    # How yt-dlp is asked to stop. SIGINT is the signal yt-dlp handles gracefully:
    # it tells its own ffmpeg to quit ("q"), which flushes the stream and exits.
    # SIGTERM kills yt-dlp outright and leaves that ffmpeg orphaned, erroring with
    # "Broken pipe" into the log once the segmenter goes away. Windows cannot
    # deliver SIGINT to a child, so it falls back to SIGTERM there.
    _YTDLP_STOP_SIGNAL = signal.SIGINT if os.name != "nt" else signal.SIGTERM

    # yt-dlp is started in its own session (process group) so that, when it has to
    # be killed, its ffmpeg child can be killed with it. Otherwise that ffmpeg is
    # orphaned holding the write end of the segmenter's pipe: the segmenter never
    # sees EOF and a process plus its sockets leak for the life of the container.
    _YTDLP_OWN_SESSION = os.name != "nt"

    # Per-run outcome, reset at the top of start().
    segments_produced: int = 0

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _begin_run(self, info: StreamInfoObject) -> None:
        """Called at the start of every run, before any process is spawned."""

    def _timestamp_segment(self, info: StreamInfoObject, seg_mtime: float, duration: float) -> float:
        """Returns the audio_start_time for a segment that ffmpeg finished writing
        at seg_mtime (epoch seconds) and that lasts `duration` seconds."""
        raise NotImplementedError

    def _after_segment(self, info: StreamInfoObject) -> None:
        """Called after a segment was queued; segments_produced already counts it."""

    def _poll_hook(self, info: StreamInfoObject) -> bool:
        """Called once per monitor iteration. Return True to end the run early."""
        return False

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][{self._NAME}] Starting")
        # The worker instance is reused across streams: reset the per-run outcome so
        # neither a stale segment count nor a stale fall-behind flag colours this run.
        self.segments_produced = 0
        self.is_slow = False
        self._begin_run(info)

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        key_dir = os.path.join(project_root, "tmp", info.key)
        segment_dir = os.path.join(key_dir, self._SEGMENT_SUBDIR)
        log_path = os.path.join(key_dir, self._LOG_FILENAME)

        if os.path.exists(segment_dir):
            shutil.rmtree(segment_dir)
        os.makedirs(segment_dir)

        # Both children append their stderr to the same per-key log. The parent's
        # handle is closed as soon as they are spawned; each child keeps its own fd.
        log_file = self._open_log(info, log_path)
        try:
            ytdlp_proc = self._create_ytdlp_process(info, log_file)
            ffmpeg_proc = None
            if ytdlp_proc is not None:
                ffmpeg_proc = self._create_ffmpeg_process(info, segment_dir, ytdlp_proc.stdout, log_file)
        finally:
            if log_file is not None:
                log_file.close()

        if ytdlp_proc is None:
            return
        if ffmpeg_proc is None:
            self._stop_ytdlp(ytdlp_proc)
            return

        # ffmpeg now owns the only read end of yt-dlp's stdout pipe. Dropping our
        # copy means yt-dlp gets EPIPE and exits if ffmpeg dies, instead of blocking
        # forever on a full pipe that nobody drains (with the monitor waiting on it).
        if ytdlp_proc.stdout is not None:
            ytdlp_proc.stdout.close()

        try:
            self._monitor_segments(info, segment_dir, ytdlp_proc, ffmpeg_proc)
        finally:
            # Sample the exit codes before stopping anything: None means a process
            # was still running (we are stopping it), not that it failed.
            ytdlp_exit = ytdlp_proc.poll()
            ffmpeg_exit = ffmpeg_proc.poll()
            # Stopping yt-dlp closes the pipe, which lets ffmpeg flush and exit cleanly.
            self._stop_ytdlp(ytdlp_proc)
            self._stop_process(ffmpeg_proc)
            self._log_outcome(info, ytdlp_exit, ffmpeg_exit, log_path)
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
    ):
        next_seq = 0
        # Stall watchdog: when no segment has become ready for stale_ytdlp_seconds
        # while yt-dlp is still alive, it is wedged (or the stream is gone without
        # yt-dlp noticing) and gets terminated so the worker can exit cleanly.
        last_segment_time = time.time()
        ytdlp_exit_time: float | None = None

        while not self.stop_event.is_set():
            if self._poll_hook(info):
                break

            seg_path = self._seg_path(segment_dir, next_seq)
            next_seg_path = self._seg_path(segment_dir, next_seq + 1)

            ytdlp_done = ytdlp_proc.poll() is not None
            ffmpeg_done = ffmpeg_proc.poll() is not None
            both_done = ytdlp_done and ffmpeg_done

            # A segment is safe to read when the next one has appeared (ffmpeg has
            # moved on) or when both upstream processes have exited (last segment).
            seg_ready = os.path.exists(seg_path) and (os.path.exists(next_seg_path) or both_done)

            if seg_ready:
                last_segment_time = time.time()
                try:
                    with open(seg_path, "rb") as f:
                        data = f.read()
                        # ffmpeg stamps a segment's mtime when it finishes writing
                        # it, i.e. when the content at the end of the segment was
                        # ingested. Read it from the open handle, before removal.
                        seg_mtime = os.fstat(f.fileno()).st_mtime
                except Exception as e:
                    logger.error(f"[{info.key}][{self._NAME}] Failed to read segment {next_seq}: {e}")
                    next_seq += 1
                    continue
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(seg_path)

                duration = StreamHelper.get_precise_duration(data)
                if duration > 0 and data:
                    if self.segments_produced == 0:
                        # One-off record of what the pipeline actually delivered: the
                        # quickest way to spot a codec the segmenter or server can't use.
                        logger.info(
                            f"[{info.key}][{self._NAME}] First segment ready: {StreamHelper.describe_codecs(data)}, {duration:.2f}s"
                        )
                    process_obj = ProcessObject(
                        raw=data,
                        audio_start_time=self._timestamp_segment(info, seg_mtime, duration),
                        key=info.key,
                        media_type=info.media_type,
                        vod_accurate=self._VOD_ACCURATE,
                    )
                    logger.debug(f"[{info.key}][{self._NAME}] Queuing segment {next_seq}. Duration: {duration:.3f}s")
                    self.queue.put(process_obj)
                    self.segments_produced += 1
                    self._after_segment(info)
                else:
                    logger.warning(f"[{info.key}][{self._NAME}] Segment {next_seq} has no usable data, skipping.")

                next_seq += 1

                if both_done and not os.path.exists(self._seg_path(segment_dir, next_seq)):
                    logger.info(f"[{info.key}][{self._NAME}] Stream ended after {next_seq} segments.")
                    break
                continue

            if both_done:
                logger.info(f"[{info.key}][{self._NAME}] Both processes exited, no more segments.")
                break

            if ffmpeg_done:
                # yt-dlp's only consumer is gone. It normally notices the closed pipe
                # and exits by itself; if it is stuck in network I/O instead, stop it.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    ytdlp_proc.wait(timeout=5)
                if ytdlp_proc.poll() is None:
                    logger.warning(
                        f"[{info.key}][{self._NAME}] ffmpeg exited (code={ffmpeg_proc.returncode}) while yt-dlp was still running; stopping yt-dlp."
                    )
                    self._stop_ytdlp(ytdlp_proc)
                continue

            if ytdlp_done:
                # ffmpeg should hit EOF on the pipe, flush its last segment and exit
                # on its own. Don't wait on it forever if it doesn't.
                if ytdlp_exit_time is None:
                    ytdlp_exit_time = time.time()
                elif time.time() - ytdlp_exit_time > self._DRAIN_TIMEOUT_SECONDS:
                    logger.warning(
                        f"[{info.key}][{self._NAME}] ffmpeg still running {self._DRAIN_TIMEOUT_SECONDS}s after yt-dlp exited; stopping it."
                    )
                    self._stop_process(ffmpeg_proc)
                    continue
                time.sleep(0.5)
                continue

            stalled_for = time.time() - last_segment_time
            if stalled_for > self.stale_ytdlp_seconds:
                logger.warning(
                    f"[{info.key}][{self._NAME}] No new segment in {stalled_for:.0f}s (yt-dlp pid={ytdlp_proc.pid}). Terminating yt-dlp to finish cleanly."
                )
                self._stop_ytdlp(ytdlp_proc)
                continue

            time.sleep(0.5)

    # ------------------------------------------------------------------
    # Process creation
    # ------------------------------------------------------------------

    def _create_ytdlp_process(self, info: StreamInfoObject, log_file: TextIO | None) -> subprocess.Popen | None:
        try:
            format_args = StreamHelper.ytdlp_format_args(info.media_type, to_stdout=True)
            cmd = [
                self.ytdlp_path,
                # Not --quiet: with "-o -" yt-dlp already sends every status line to
                # stderr, and those lines are the diagnostics we want on disk (which
                # formats were selected, "does not pass filter", warnings, errors).
                # --quiet would hide all but the errors. Only the progress bar goes.
                "--no-progress",
                *self._YTDLP_EXTRA_ARGS,
                *StreamHelper.ytdlp_auth_args(info.url, purpose="download"),
                *format_args,
                "--downloader-args",
                self._FFMPEG_DOWNLOADER_ARGS,
                "-o",
                "-",
                info.url,
            ]
            self._write_log(log_file, f"yt-dlp: {shlex.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=self._stderr_target(log_file),
                start_new_session=self._YTDLP_OWN_SESSION,
            )
            logger.debug(f"[{info.key}][{self._NAME}] yt-dlp started ({shlex.join(self._YTDLP_EXTRA_ARGS + tuple(format_args))})")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][{self._NAME}] Failed to start yt-dlp: {e}")
            return None

    def _create_ffmpeg_process(
        self,
        info: StreamInfoObject,
        segment_dir: str,
        stdin,
        log_file: TextIO | None,
    ) -> subprocess.Popen | None:
        try:
            output_pattern = os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}%06d{self._SEGMENT_EXT}")
            cmd = [
                "ffmpeg",
                "-y",
                # Only warnings and errors: the info-level "Opening ... for writing"
                # line per segment and the progress line would flood the log.
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "warning",
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
                # Explicit PCR period (20 ms is the muxer's own default). Without it
                # the mpegts muxer warns "frame size not set" once per segment on
                # audio-only input, which would flood the run log.
                "-segment_format_options",
                "pcr_period=20",
                "-reset_timestamps",
                "1",
                output_pattern,
            ]
            self._write_log(log_file, f"ffmpeg: {shlex.join(cmd)}")
            proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.DEVNULL, stderr=self._stderr_target(log_file))
            logger.debug(f"[{info.key}][{self._NAME}] ffmpeg segmenter started (target segment: {self.buffer_size_seconds}s)")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][{self._NAME}] Failed to start ffmpeg: {e}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_log(self, info: StreamInfoObject, log_path: str) -> TextIO | None:
        try:
            return StreamHelper.open_process_log(log_path, self._NAME, info.stream_id)
        except OSError as e:
            logger.warning(f"[{info.key}][{self._NAME}] Cannot open {log_path} ({e}); yt-dlp/ffmpeg output will be discarded.")
            return None

    @staticmethod
    def _write_log(log_file: TextIO | None, line: str) -> None:
        """Writes one line to the run log ahead of the child processes' own output."""
        if log_file is not None:
            log_file.write(line + "\n")
            log_file.flush()

    @staticmethod
    def _stderr_target(log_file: TextIO | None):
        return log_file if log_file is not None else subprocess.DEVNULL

    @staticmethod
    def _append_log(path: str, line: str) -> None:
        with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _stop_ytdlp(self, proc: subprocess.Popen) -> None:
        self._stop_process(proc, self._YTDLP_STOP_SIGNAL, own_session=self._YTDLP_OWN_SESSION)

    @staticmethod
    def _wait(proc: subprocess.Popen, timeout: float) -> bool:
        """True once the process has exited, False if it is still running after timeout."""
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    @classmethod
    def _stop_process(cls, proc: subprocess.Popen, sig: int = signal.SIGTERM, own_session: bool = False) -> None:
        """Asks a child to stop with `sig`, escalating to SIGKILL if it does not
        exit within a few seconds. `own_session` marks a child started with
        start_new_session=True: the kill then goes to its whole process group, so
        its own children die with it instead of being orphaned. No-op if the child
        has already exited."""
        if proc.poll() is not None:
            return
        proc.send_signal(sig)
        if cls._wait(proc, 5):
            return
        if sig == signal.SIGINT:
            # yt-dlp's first SIGINT asks its ffmpeg to quit ("q"), which a download
            # stalled in a socket read never acts on. A second SIGINT lands in
            # yt-dlp's interrupt handler, which kills that ffmpeg and exits.
            proc.send_signal(sig)
            if cls._wait(proc, 5):
                return
        logger.debug(f"[{cls._NAME}] pid {proc.pid} ignored signal {sig}; killing it.")
        if own_session and os.name != "nt":
            # The session leader's pid is its process group id.
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
        cls._wait(proc, 5)

    def _log_outcome(self, info: StreamInfoObject, ytdlp_exit: int | None, ffmpeg_exit: int | None, log_path: str) -> None:
        """Records how the run ended, in the app log and as a footer in the per-key
        log. Exit codes are the ones sampled before the worker stopped the
        processes; None means that process was still running at that point."""

        def fmt(code: int | None) -> str:
            return "running" if code is None else str(code)

        summary = f"yt-dlp exit={fmt(ytdlp_exit)}, ffmpeg exit={fmt(ffmpeg_exit)}, segments={self.segments_produced}"
        # A very long run can outgrow the log between the trims done at run start.
        StreamHelper.trim_log_file(log_path)
        self._append_log(log_path, f"--- {self._NAME} stopped at {time.strftime('%Y-%m-%d %H:%M:%S')} ({summary}) ---")

        if self.stop_event.is_set():
            # Deliberate shutdown or restart: not a failure, whatever the exit codes.
            logger.info(f"[{info.key}][{self._NAME}] Stopped on request ({summary}).")
        elif self.is_slow:
            logger.info(f"[{info.key}][{self._NAME}] Stopped to switch to the live edge ({summary}).")
        elif self.segments_produced == 0:
            logger.warning(f"[{info.key}][{self._NAME}] Run produced no segments ({summary}); see {log_path} for details.")
        elif ytdlp_exit not in (None, 0):
            logger.warning(
                f"[{info.key}][{self._NAME}] yt-dlp exited with an error ({summary}); the stream may simply have ended, see {log_path} for details."
            )
        else:
            logger.info(f"[{info.key}][{self._NAME}] Run finished ({summary}).")

    def _seg_path(self, segment_dir: str, seq: int) -> str:
        return os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}{seq:06d}{self._SEGMENT_EXT}")
