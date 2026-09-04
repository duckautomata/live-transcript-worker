import logging
import time
from collections.abc import Callable

from live_transcript_worker.custom_types import StreamInfoObject
from live_transcript_worker.worker_segment_pipeline import SegmentPipelineWorker

logger = logging.getLogger(__name__)


class TwitchLFSWorker(SegmentPipelineWorker):
    """
    Worker for Twitch streams using --live-from-start.

    yt-dlp pipes the stream to ffmpeg, which writes keyframe-aligned MPEG-TS
    segments to a directory. Each segment is independently decodable (starts at
    an IDR with SPS/PPS) and has accurate duration metadata — eliminating black
    frames and timestamp drift that arise from manual byte-level splitting. See
    SegmentPipelineWorker for the pipeline, its diagnostics
    (tmp/{key}/twitch_lfs.log) and its process handling.

    Timestamps are derived from info.start_time plus the accumulated duration of
    all previously emitted segments, measured on the actual segment bytes.

    --live-from-start only works for channels whose VODs are enabled (yt-dlp
    downloads the in-progress VOD); otherwise yt-dlp exits at once with
    "there are no formats that can be downloaded from the start", the run
    produces no segments, and Worker._start_twitch falls back to
    LiveSegmentWorker.
    """

    _NAME = "TwitchLFSWorker"
    _SEGMENT_SUBDIR = "lfs_segments"
    _LOG_FILENAME = "twitch_lfs.log"
    _YTDLP_EXTRA_ARGS = ("--live-from-start",)
    _VOD_ACCURATE = True

    # Per-run state, reset in _begin_run(). segments_produced (from the base class)
    # lets Worker._start_twitch tell a real capture from a fast --live-from-start
    # failure; _on_first_segment fires once so the caller can persist progress only
    # after the first segment is captured.
    _on_first_segment: Callable[[], None] | None = None
    _audio_start_time: float = 0.0

    def start(self, info: StreamInfoObject, on_first_segment: Callable[[], None] | None = None):
        self._on_first_segment = on_first_segment
        super().start(info)

    def _begin_run(self, info: StreamInfoObject) -> None:
        try:
            self._audio_start_time = float(info.start_time)
        except (ValueError, TypeError):
            self._audio_start_time = time.time()
            logger.warning(f"[{info.key}][TwitchLFSWorker] Invalid start_time, defaulting to system time.")

    def _timestamp_segment(self, info: StreamInfoObject, seg_mtime: float, duration: float) -> float:
        start = self._audio_start_time
        self._audio_start_time += duration
        return start

    def _after_segment(self, info: StreamInfoObject) -> None:
        if self.segments_produced == 1 and self._on_first_segment is not None:
            # Persist progress (the stream id) only once LFS has proven it can
            # capture this stream — keeps a fast-failed attempt retryable while
            # staying crash-safe mid-stream.
            try:
                self._on_first_segment()
            except Exception as e:
                logger.warning(f"[{info.key}][TwitchLFSWorker] on_first_segment callback failed: {e}")

    def _poll_hook(self, info: StreamInfoObject) -> bool:
        # Check if worker is too far behind live
        gap = time.time() - self._audio_start_time
        if gap > self.stale_lfs_gap_seconds:
            logger.warning(
                f"[{info.key}][TwitchLFSWorker] Worker is {gap / 60:.1f} minutes behind live "
                f"(threshold: {self.stale_lfs_gap_seconds / 60:.1f} min). Switching to LiveSegmentWorker."
            )
            self.is_slow = True
            return True
        return False
