import logging
import subprocess
import time

from src.live_transcript_worker.custom_types import ProcessObject, StreamInfoObject
from src.live_transcript_worker.worker_abstract import AbstractWorker

logger = logging.getLogger(__name__)


class MPEGFixedBitrateWorker(AbstractWorker):
    """
    Worker that reads in a MPEG-TS stream at a fixed bitrate.

    Pros:
        Simple, almost always works. Required for twitch streams
    Cons:
        Timestamp accuracy is not perfect, especially when there is a large delay, or when the stream goes down in the middle.
    """

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][MPEGFixedBitrateWorker] Starting download")
        process, sample_rate = self.create_process(info)

        if process is None:
            logger.error(f"[{info.key}][MPEGFixedBitrateWorker] process failed to start.")
            return

        if process.stdout is None or process.stderr is None:
            logger.error(f"[{info.key}][MPEGFixedBitrateWorker] yt-dlp process failed to start.")
            return

        buffer: bytes = b""
        chunk_size = self.buffer_size_seconds * sample_rate
        audio_start_time = time.time() - self.live_latency_seconds
        next_start_time = 0
        while not self.stop_event.is_set():
            chunk = process.stdout.read(4096)
            next_start_time = (
                time.time() - self.live_latency_seconds
            )  # Once we process the buffer, the current time is when the next buffer starts
            if not chunk:
                process.poll()
                if process.returncode is not None:
                    logger.info(f"[{info.key}][MPEGFixedBitrateWorker] yt-dlp process ended with code {process.returncode}.")
                    stderr_output = process.stderr.read().decode(errors="ignore")
                    if stderr_output:
                        logger.debug(f"[{info.key}][MPEGFixedBitrateWorker] yt-dlp stderr:\n{stderr_output}")
                    if process.returncode != 0:
                        logger.error(f"[{info.key}][MPEGFixedBitrateWorker] yt-dlp exited with an error.")
                    else:
                        logger.info(f"[{info.key}][MPEGFixedBitrateWorker] yt-dlp process finished (stream likely ended).")
                else:
                    logger.warning(f"[{info.key}][MPEGFixedBitrateWorker] No data from yt-dlp, potentially stalled...")
                break

            buffer += chunk
            if len(buffer) < chunk_size:
                continue

            process_obj = ProcessObject(
                raw=buffer,
                audio_start_time=audio_start_time,
                key=info.key,
                media_type=info.media_type,
            )
            logger.debug(f"[{info.key}][MPEGFixedBitrateWorker] Adding audio to queue.")
            self.queue.put(process_obj)
            audio_start_time = next_start_time
            buffer = b""

        if len(buffer) >= 4096:
            # Exited but there is still data in the buffer.
            process_obj = ProcessObject(
                raw=buffer,
                audio_start_time=audio_start_time,
                key=info.key,
                media_type=info.media_type,
            )
            logger.debug(f"[{info.key}][MPEGFixedBitrateWorker] Adding final audio to queue.")
            self.queue.put(process_obj)

        return

    def create_process(self, info: StreamInfoObject) -> tuple[subprocess.Popen[bytes] | None, int]:
        process = None
        sample_rate = 0
        logger.debug(f"[{info.key}][MPEGFixedBitrateWorker][create_process] creating yt-dlp download process")
        try:
            cmd = [
                f"{self.ytdlp_path}",
                "-f",
                "ba",
                "--quiet",
                "--no-warnings",
                # "--retries",
                # "10",
                # "--fragment-retries",
                # "10",
                "--match-filter",
                "is_live",
                "-o",
                "-",
                info.url,
            ]
            if "twitch.tv" in info.url.lower():
                sample_rate = self.twitch_audio_rate
            else:
                sample_rate = self.yt_audio_rate
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            logger.debug(f"[{info.key}][MPEGFixedBitrateWorker][create_process] successfully created yt-dlp download process.")
            return process, sample_rate
        except FileNotFoundError:
            logger.error(f"[{info.key}][MPEGFixedBitrateWorker][create_process] 'yt-dlp' not found under '{self.ytdlp_path}'.")
        return process, sample_rate
