import logging
from queue import Queue
from threading import Event
from src.live_transcript_worker.worker_buffered import MPEGBufferedWorker
from src.live_transcript_worker.worker_fixedbitrate import MPEGFixedBitrateWorker
from src.live_transcript_worker.worker_dash import DASHWorker
from src.live_transcript_worker.custom_types import StreamInfoObject

logger = logging.getLogger(__name__)

class Worker:
    """Main worker class. Manages all concrete worker classes.
    On start(), it will determine which concrete worker to use.
    """

    def __init__(self, key: str, queue: Queue, stop_event: Event):
        self.mpeg_fixed_bitrate_worker = MPEGFixedBitrateWorker(key, queue, stop_event)
        self.mpeg_buffered_worker = MPEGBufferedWorker(key, queue, stop_event)
        self.dash_worker = DASHWorker(key, queue, stop_event)

    def start(self, info: StreamInfoObject):
        # Buffered_worder grabs video, fixed_bitrate grabs audio, dash can grab both video and audio only.
        # Twitch requires fixed bitrate. Youtube requires video. DASH only works on YouTube.
        if "twitch.tv" in info.url.lower():
            self.mpeg_fixed_bitrate_worker.start(info)
        elif "youtube.com" in info.url.lower() or "youtu.be" in info.url.lower():
            self.dash_worker.start(info)
        else:
            self.mpeg_buffered_worker.start(info)
