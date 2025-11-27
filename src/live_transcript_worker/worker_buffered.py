import logging
import subprocess
from threading import Event, Lock, Thread
import time
from src.live_transcript_worker.worker_abstract import AbstractWorker

from src.live_transcript_worker.helper import StreamHelper
from src.live_transcript_worker.types import ProcessObject, StreamInfoObject

logger = logging.getLogger(__name__)

class MPEGBufferedWorker(AbstractWorker):
    """
    Worker that reads in a MPEG-TS stream into a buffer. Then reads from the buffer every n seconds.
    The main difference between this and FixedBitrate is that this goes off of time, and fixedbitrate goes off of the number of bytes.

    Pros:
        Works with variable bitrate. Meaning it supports video and audio.
    Cons:
        Cannot detect injected video ads.
    """

    def start(self, info: StreamInfoObject):
        self.ytdlp_stopped = Event()
        self.buffer_lock = Lock()
        self.buffer = bytearray()
        download_thread = Thread(target=self.downloader, args=(info,), daemon=True)
        download_thread.start()

        audio_start_time = time.time() - self.live_latency_seconds
        next_start_time = 0
        min_buffer_size = 8192
        should_sleep = False
        logger.info(f"[{info.key}][MPEGBufferedWorker] Starting buffer reader")
        while not self.stop_event.is_set() and not self.ytdlp_stopped.is_set():
            if should_sleep:
                time.sleep(1)
                should_sleep = False
            with self.buffer_lock:
                next_start_time = time.time() - self.live_latency_seconds  # Once we process the buffer, the current time is when the next buffer starts
                if not self.buffer:
                    should_sleep = True
                    continue
                buffer_copy = bytes(self.buffer)
                if len(buffer_copy) < min_buffer_size or StreamHelper.get_duration(buffer_copy) < self.buffer_size_seconds:
                    should_sleep = True
                    continue

                process_obj = ProcessObject(
                    raw=buffer_copy,
                    audio_start_time=audio_start_time,
                    key=info.key,
                    media_type=info.media_type,
                )
                logger.debug(f"[{info.key}][MPEGBufferedWorker] Adding audio to queue.")
                self.queue.put(process_obj)
                audio_start_time = next_start_time
                self.buffer.clear()

        with self.buffer_lock:
            buffer_copy = bytes(self.buffer)
            if len(buffer_copy) >= min_buffer_size:
                # Exited but there is still data in the buffer.
                process_obj = ProcessObject(
                    raw=buffer_copy,
                    audio_start_time=audio_start_time,
                    key=info.key,
                    media_type=info.media_type,
                )
                logger.debug(f"[{info.key}][MPEGBufferedWorker] Adding final audio to queue.")
                self.queue.put(process_obj)

        download_thread.join()
        return

    
    def downloader(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][MPEGBufferedWorker] Starting downloader")
        process = self.create_process(info)

        if process is None:
            logger.error(f"[{info.key}][MPEGBufferedWorker] process failed to start.")
            self.ytdlp_stopped.set()
            return

        if process.stdout is None or process.stderr is None:
            logger.error(f"[{info.key}][MPEGBufferedWorker] yt-dlp process failed to start.")
            self.ytdlp_stopped.set()
            return

        while not self.stop_event.is_set():
            chunk = process.stdout.read(4096)
            if not chunk:
                process.poll()
                if process.returncode is not None:
                    logger.info(f"[{info.key}][MPEGBufferedWorker] yt-dlp process ended with code {process.returncode}.")
                    stderr_output = process.stderr.read().decode(errors="ignore")
                    if stderr_output:
                        logger.debug(f"[{info.key}][MPEGBufferedWorker] yt-dlp stderr:\n{stderr_output}")
                    if process.returncode != 0:
                        logger.error(f"[{info.key}][MPEGBufferedWorker] yt-dlp exited with an error.")
                    else:
                        logger.info(f"[{info.key}][MPEGBufferedWorker] yt-dlp process finished (stream likely ended).")
                else:
                    logger.warning(f"[{info.key}][MPEGBufferedWorker] No data from yt-dlp, potentially stalled...")
                break

            with self.buffer_lock:
                self.buffer.extend(chunk)

        logger.info(f"[{info.key}][MPEGBufferedWorker] Stopping downloader")
        self.ytdlp_stopped.set()
        return

    def create_process(
        self, info: StreamInfoObject
    ) -> subprocess.Popen[bytes] | None:
        process = None
        logger.debug(f"[{info.key}][MPEGBufferedWorker][create_process] creating yt-dlp download process")
        try:
            cmd = [
                f"{self.ytdlp_path}",
                "--quiet",
                "--no-warnings",
                "--match-filter",
                "is_live",
                "-o",
                "-",
                info.url,
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
            logger.debug(f"[{info.key}][MPEGBufferedWorker][create_process] successfully created yt-dlp download process.")
            return process
        except FileNotFoundError:
            logger.error(f"[{info.key}][MPEGBufferedWorker][create_process] 'yt-dlp' not found under '{self.ytdlp_path}'.")
        return process
