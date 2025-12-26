import os
from abc import ABC, abstractmethod
from queue import Queue
from threading import Event

from src.live_transcript_worker.config import Config
from src.live_transcript_worker.custom_types import StreamInfoObject


class AbstractWorker(ABC):
    """
    Abstract Class used to work on a specific url
    """

    def __init__(self, key: str, queue: Queue, stop_event: Event):
        self.key = key
        self.queue = queue
        self.stop_event = stop_event

        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        self.ytdlp_path = os.path.join(project_root_dir, "bin", "yt-dlp")
        self.buffer_size_seconds: int = Config.get_server_config().get("buffer_size_seconds", 6)

        # 1 fragment = 1 second
        self.dash_stale_size = 60

        self.yt_audio_rate = 20_000
        self.ty_video_rate = 1_028_571
        self.twitch_audio_rate = 25_540
        self.twitch_sl_audio_rate = 30_117

        self.live_latency_seconds = 1

    @abstractmethod
    def start(self, info: StreamInfoObject) -> None:
        """Starts working on the given url."""
        pass
