import logging
import os
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

    def start(self, info: StreamInfoObject):
        live_from_start = Config.get_streamer_config(self.key).get("live_from_start", True)

        url = info.url.lower()
        is_twitch = "twitch.tv" in url
        is_youtube = "youtube.com" in url or "youtu.be" in url

        if not live_from_start:
            logger.info(f"[{self.key}][Worker] live_from_start=false, using LiveSegmentWorker")
            self.live_segment_worker.start(info)
        elif is_twitch:
            last_id = self._read_lfs_stream_id()
            if last_id == info.stream_id:
                # Same stream restarted mid-way: TwitchLFSWorker would replay from the
                # beginning and produce duplicate lines. Fall back to LiveSegmentWorker
                # to join at the live edge instead.
                logger.info(
                    f"[{self.key}][Worker] Twitch restart on same stream id '{info.stream_id}', using LiveSegmentWorker to avoid duplicates"
                )
                self.live_segment_worker.start(info)
            else:
                logger.info(f"[{self.key}][Worker] Twitch new stream id '{info.stream_id}', using TwitchLFSWorker")
                self._write_lfs_stream_id(info.stream_id)
                self.twitch_lfs_worker.start(info)
        elif is_youtube:
            self.dash_worker.start(info)
        else:
            logger.info(f"[{self.key}][Worker] Non-Twitch/YouTube URL with live_from_start=true, using LiveSegmentWorker")
            self.live_segment_worker.start(info)
