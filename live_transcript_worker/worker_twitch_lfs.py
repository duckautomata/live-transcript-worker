import contextlib
import logging
import os
import shutil
import subprocess
import time

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

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][TwitchLFSWorker] Starting")

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

        ytdlp_proc = self._create_ytdlp_process(info)
        if ytdlp_proc is None:
            return

        ffmpeg_proc = self._create_ffmpeg_process(info, segment_dir, ytdlp_proc.stdout)
        if ffmpeg_proc is None:
            ytdlp_proc.terminate()
            return

        try:
            self._monitor_segments(info, segment_dir, ytdlp_proc, ffmpeg_proc, audio_start_time)
        finally:
            # Terminating yt-dlp closes the pipe, which causes ffmpeg to flush and exit cleanly.
            for proc in (ytdlp_proc, ffmpeg_proc):
                if proc.poll() is None:
                    proc.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
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

    def _create_ytdlp_process(self, info: StreamInfoObject) -> subprocess.Popen | None:
        try:
            fmt_selector = "best" if info.media_type == Media.VIDEO else "ba/best"
            cmd = [
                self.ytdlp_path,
                "--quiet",
                "--no-warnings",
                "--live-from-start",
                *StreamHelper.ytdlp_auth_args(info.url, purpose="download"),
                "-f",
                fmt_selector,
                "-o",
                "-",
                info.url,
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            logger.debug(f"[{info.key}][TwitchLFSWorker] yt-dlp started (format: {fmt_selector})")
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

    def _seg_path(self, segment_dir: str, seq: int) -> str:
        return os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}{seq:06d}{self._SEGMENT_EXT}")
