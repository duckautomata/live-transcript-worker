import os
from abc import ABC, abstractmethod
from queue import Queue
from threading import Event

from live_transcript_worker.config import Config
from live_transcript_worker.custom_types import StreamInfoObject


class AbstractWorker(ABC):
    """
    Abstract Class used to work on a specific url
    """

    def __init__(self, key: str, queue: Queue, stop_event: Event):
        self.key = key
        self.queue = queue
        self.stop_event = stop_event

        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.ytdlp_path = os.path.join(project_root_dir, "bin", "yt-dlp")
        self.buffer_size_seconds: int = Config.get_server_config().get("buffer_size_seconds", 6)

        # Stale-detection thresholds (all in seconds).
        stale_cfg: dict = Config.get_server_config().get("stale_threshold", {}) or {}
        # How long to wait for a DASH fragment's tracks to be complete before emitting partial data.
        self.stale_fragment_seconds: int = stale_cfg.get("fragment_seconds", 60)
        # Gap (seconds behind live) at which we fall back to LiveSegmentWorker.
        self.stale_lfs_gap_seconds: int = stale_cfg.get("lfs_gap_seconds", 600)
        # No-new-fragment timeout after which we terminate yt-dlp.
        self.stale_ytdlp_seconds: int = stale_cfg.get("ytdlp_seconds", 180)

        self.yt_audio_rate = 20_000
        self.ty_video_rate = 1_028_571
        self.twitch_audio_rate = 25_540
        self.twitch_sl_audio_rate = 30_117

        self.live_latency_seconds = 1

        self.is_slow: bool = False

    @abstractmethod
    def start(self, info: StreamInfoObject) -> None:
        """Starts working on the given url."""
        pass
