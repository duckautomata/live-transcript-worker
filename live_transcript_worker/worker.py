import logging
from queue import Queue
from threading import Event

from live_transcript_worker.custom_types import StreamInfoObject
from live_transcript_worker.worker_buffered import MPEGBufferedWorker
from live_transcript_worker.worker_dash import DASHWorker
from live_transcript_worker.worker_fixedbitrate import MPEGFixedBitrateWorker
from live_transcript_worker.worker_twitch_lfs import TwitchLFSWorker

logger = logging.getLogger(__name__)


class Worker:
    """Main worker class. Manages all concrete worker classes.
    On start(), it will determine which concrete worker to use.
    """

    def __init__(self, key: str, queue: Queue, stop_event: Event):
        self.mpeg_fixed_bitrate_worker = MPEGFixedBitrateWorker(key, queue, stop_event)
        self.mpeg_buffered_worker = MPEGBufferedWorker(key, queue, stop_event)
        self.dash_worker = DASHWorker(key, queue, stop_event)
        self.twitch_lfs_worker = TwitchLFSWorker(key, queue, stop_event)

    def start(self, info: StreamInfoObject):
        # Buffered_worker grabs video, fixed_bitrate grabs audio, dash can grab both video and audio only.
        if "twitch.tv" in info.url.lower():
            self.twitch_lfs_worker.start(info)
        elif "youtube.com" in info.url.lower() or "youtu.be" in info.url.lower():
            self.dash_worker.start(info)
        else:
            self.mpeg_buffered_worker.start(info)
