import json
import logging
import os
import time
from queue import Queue
from threading import Event

from live_transcript_worker.config import Config
from live_transcript_worker.custom_types import StreamInfoObject
from live_transcript_worker.worker_buffered import MPEGBufferedWorker
from live_transcript_worker.worker_dash import DASHWorker
from live_transcript_worker.worker_fixedbitrate import MPEGFixedBitrateWorker
from live_transcript_worker.worker_live_segment import LiveSegmentWorker
from live_transcript_worker.worker_twitch_lfs import TwitchLFSWorker

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Worker:
    """Main worker class. Manages all concrete worker classes.
    On start(), it will determine which concrete worker to use.
    """

    def __init__(self, key: str, queue: Queue, stop_event: Event):
        self.key = key
        self.mpeg_fixed_bitrate_worker = MPEGFixedBitrateWorker(key, queue, stop_event)
        self.mpeg_buffered_worker = MPEGBufferedWorker(key, queue, stop_event)
        self.dash_worker = DASHWorker(key, queue, stop_event)
        self.twitch_lfs_worker = TwitchLFSWorker(key, queue, stop_event)
        self.live_segment_worker = LiveSegmentWorker(key, queue, stop_event)

    # ------------------------------------------------------------------
    # Twitch LFS stream-id persistence
    # ------------------------------------------------------------------

    def _lfs_id_path(self) -> str:
        return os.path.join(_PROJECT_ROOT, "tmp", self.key, "twitch_lfs_stream_id")

    def _read_lfs_stream_id(self) -> str | None:
        try:
            with open(self._lfs_id_path()) as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def _write_lfs_stream_id(self, stream_id: str) -> None:
        path = self._lfs_id_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(stream_id)

    # ------------------------------------------------------------------
    # Slow worker / gap detection
    # ------------------------------------------------------------------

    def _dash_state_path(self) -> str:
        return os.path.join(_PROJECT_ROOT, "tmp", self.key, "dash_state.json")

    def _get_gap_minutes(self, info: StreamInfoObject, is_youtube: bool) -> float | None:
        """Returns the gap in minutes between where the worker would resume and live.
        Returns None if start_time is invalid."""
        try:
            start_time = float(info.start_time)
        except (ValueError, TypeError):
            return None

        resume_time: float = start_time

        if is_youtube:
            # Check if we have saved state for this stream (resuming)
            state_path = self._dash_state_path()
            if os.path.exists(state_path):
                try:
                    with open(state_path) as f:
                        data = json.load(f)
                    if data.get("stream_id") == info.stream_id:
                        resume_time = data.get("current_stream_time", start_time)
                except Exception:
                    pass

        gap_seconds = time.time() - resume_time
        return gap_seconds / 60.0

    # ------------------------------------------------------------------

    def start(self, info: StreamInfoObject):
        live_from_start = Config.get_streamer_config(self.key).get("live_from_start", True)

        url = info.url.lower()
        is_twitch = "twitch.tv" in url
        is_youtube = "youtube.com" in url or "youtu.be" in url

        if not live_from_start:
            logger.info(f"[{self.key}][Worker] live_from_start=false, using LiveSegmentWorker")
            self.live_segment_worker.start(info)
        elif is_twitch:
            self._start_twitch(info)
        elif is_youtube:
            self._start_youtube(info)
        else:
            logger.info(f"[{self.key}][Worker] Non-Twitch/YouTube URL with live_from_start=true, using LiveSegmentWorker")
            self.live_segment_worker.start(info)

    def _start_twitch(self, info: StreamInfoObject):
        slow_worker_threshold = Config.get_server_config().get("slow_worker_threshold", 10)

        last_id = self._read_lfs_stream_id()
        if last_id == info.stream_id:
            # Same stream restarted mid-way: TwitchLFSWorker would replay from the
            # beginning and produce duplicate lines. Fall back to LiveSegmentWorker
            # to join at the live edge instead.
            logger.info(
                f"[{self.key}][Worker] Twitch restart on same stream id '{info.stream_id}', using LiveSegmentWorker to avoid duplicates"
            )
            self.live_segment_worker.start(info)
            return

        self._write_lfs_stream_id(info.stream_id)

        # Check if the gap from start to live is too large
        gap_minutes = self._get_gap_minutes(info, is_youtube=False)
        if gap_minutes is not None and gap_minutes > slow_worker_threshold:
            logger.warning(
                f"[{self.key}][Worker] Twitch stream is {gap_minutes:.1f} minutes behind live "
                f"(threshold: {slow_worker_threshold} min). Using LiveSegmentWorker instead of TwitchLFSWorker."
            )
            self.live_segment_worker.start(info)
            return

        logger.info(f"[{self.key}][Worker] Twitch new stream id '{info.stream_id}', using TwitchLFSWorker")
        self.twitch_lfs_worker.start(info)

        # If the worker fell behind during processing, switch to LiveSegmentWorker
        if self.twitch_lfs_worker.is_slow:
            logger.info(f"[{self.key}][Worker] TwitchLFSWorker fell behind, switching to LiveSegmentWorker")
            self.twitch_lfs_worker.is_slow = False
            self.live_segment_worker.start(info)

    def _start_youtube(self, info: StreamInfoObject):
        slow_worker_threshold = Config.get_server_config().get("slow_worker_threshold", 10)

        # Check if the gap from start (or resume point) to live is too large
        gap_minutes = self._get_gap_minutes(info, is_youtube=True)
        if gap_minutes is not None and gap_minutes > slow_worker_threshold:
            logger.warning(
                f"[{self.key}][Worker] YouTube stream is {gap_minutes:.1f} minutes behind live "
                f"(threshold: {slow_worker_threshold} min). Using LiveSegmentWorker instead of DASHWorker."
            )
            self.live_segment_worker.start(info)
            return

        self.dash_worker.start(info)

        # If the worker fell behind during processing, switch to LiveSegmentWorker
        if self.dash_worker.is_slow:
            logger.info(f"[{self.key}][Worker] DASHWorker fell behind, switching to LiveSegmentWorker")
            self.dash_worker.is_slow = False
            self.live_segment_worker.start(info)
