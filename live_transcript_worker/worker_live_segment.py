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


class LiveSegmentWorker(AbstractWorker):
    """
    General-purpose live stream worker for any platform supported by yt-dlp.

    yt-dlp pipes the stream to ffmpeg, which writes keyframe-aligned MPEG-TS
    segments to a directory. Each segment is independently decodable and has
    accurate duration metadata.

    Unlike TwitchLFSWorker, this worker joins the stream at the live edge
    (no --live-from-start), so there is no complete buffer of the full stream.
    Timestamps are approximated: audio_start_time is set to the wall-clock time
    when the first segment is ready, and each subsequent segment advances by its
    measured duration.

    Supports VIDEO (video+audio mux) or AUDIO-only streams depending on
    info.media_type.
    """

    _SEGMENT_PREFIX = "chunk"
    _SEGMENT_EXT = ".ts"

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][LiveSegmentWorker] Starting")

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        segment_dir = os.path.join(project_root, "tmp", info.key, "live_segments")

        if os.path.exists(segment_dir):
            shutil.rmtree(segment_dir)
        os.makedirs(segment_dir)

        ytdlp_proc = self._create_ytdlp_process(info)
        if ytdlp_proc is None:
            return

        ffmpeg_proc = self._create_ffmpeg_process(info, segment_dir, ytdlp_proc.stdout)
        if ffmpeg_proc is None:
            ytdlp_proc.terminate()
            return

        try:
            self._monitor_segments(info, segment_dir, ytdlp_proc, ffmpeg_proc)
        finally:
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
    ):
        next_seq = 0
        # Timestamp is approximated from wall-clock time at the moment the
        # first segment is emitted. This anchors us to roughly the live edge.
        audio_start_time: float | None = None

        while not self.stop_event.is_set():
            seg_path = self._seg_path(segment_dir, next_seq)
            next_seg_path = self._seg_path(segment_dir, next_seq + 1)

            both_done = ytdlp_proc.poll() is not None and ffmpeg_proc.poll() is not None

            seg_ready = os.path.exists(seg_path) and (os.path.exists(next_seg_path) or both_done)

            if seg_ready:
                # Anchor timestamp to wall clock on the first segment,
                # compensating for the segment buffer and platform latency.
                if audio_start_time is None:
                    audio_start_time = time.time() - self.buffer_size_seconds - self.live_latency_seconds
                    logger.debug(f"[{info.key}][LiveSegmentWorker] Anchoring timestamp to {audio_start_time:.3f}")

                try:
                    with open(seg_path, "rb") as f:
                        data = f.read()
                except Exception as e:
                    logger.error(f"[{info.key}][LiveSegmentWorker] Failed to read segment {next_seq}: {e}")
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
                        vod_accurate=False,
                    )
                    logger.debug(f"[{info.key}][LiveSegmentWorker] Queuing segment {next_seq}. Duration: {duration:.3f}s")
                    self.queue.put(process_obj)
                    audio_start_time += duration
                else:
                    logger.warning(f"[{info.key}][LiveSegmentWorker] Segment {next_seq} has no usable data, skipping.")

                next_seq += 1

                if both_done and not os.path.exists(self._seg_path(segment_dir, next_seq)):
                    logger.info(f"[{info.key}][LiveSegmentWorker] Stream ended after {next_seq} segments.")
                    break
            else:
                if both_done and not os.path.exists(seg_path):
                    logger.info(f"[{info.key}][LiveSegmentWorker] Both processes exited, no more segments.")
                    break
                time.sleep(0.5)

    # ------------------------------------------------------------------
    # Process creation
    # ------------------------------------------------------------------

    def _create_ytdlp_process(self, info: StreamInfoObject) -> subprocess.Popen | None:
        try:
            # Force H.264 (avc) + AAC (mp4a) so ffmpeg -c copy -f mpegts produces a valid MPEG-TS.
            if info.media_type == Media.VIDEO:
                fmt_selector = "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best"
            else:
                fmt_selector = "bestaudio[acodec^=mp4a]/ba/best"
            cmd = [
                self.ytdlp_path,
                "--quiet",
                "--no-warnings",
                *StreamHelper.ytdlp_auth_args(info.url),
                "-f",
                fmt_selector,
                "-o",
                "-",
                info.url,
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            logger.debug(f"[{info.key}][LiveSegmentWorker] yt-dlp started (format: {fmt_selector})")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][LiveSegmentWorker] Failed to start yt-dlp: {e}")
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
            logger.debug(f"[{info.key}][LiveSegmentWorker] ffmpeg segmenter started (target segment: {self.buffer_size_seconds}s)")
            return proc
        except Exception as e:
            logger.error(f"[{info.key}][LiveSegmentWorker] Failed to start ffmpeg: {e}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _seg_path(self, segment_dir: str, seq: int) -> str:
        return os.path.join(segment_dir, f"{self._SEGMENT_PREFIX}{seq:06d}{self._SEGMENT_EXT}")
