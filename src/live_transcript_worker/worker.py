from abc import ABC, abstractmethod
import logging
import os
import shutil
import glob
import re
import json
import av
from queue import Queue
import subprocess
from threading import Event, Lock, Thread
import time

from src.live_transcript_worker.helper import StreamHelper
from src.live_transcript_worker.types import Media, ProcessObject, StreamInfoObject
from src.live_transcript_worker.config import Config

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
        # Buffered_worder grabs video, fixed_bitrate grabs audio.
        # Twitch requires fixed bitrate. Youtube requires video.
        if "twitch.tv" in info.url.lower():
            self.mpeg_fixed_bitrate_worker.start(info)
        elif "youtube.com" in info.url.lower() or "youtu.be" in info.url.lower():
            self.dash_worker.start(info)
        else:
            self.mpeg_buffered_worker.start(info)


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
        self.buffer_size_seconds: int = Config.get_server_config().get(
            "buffer_size_seconds", 6
        )
        self.yt_audio_rate = 20_000
        self.ty_video_rate = 1_028_571
        self.twitch_audio_rate = 25_540
        self.twitch_sl_audio_rate = 30_117

        self.live_latency_seconds = 1

    @abstractmethod
    def start(self, info: StreamInfoObject) -> None:
        """Starts working on the given url.
        """
        pass


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

        buffer: bytes = b''
        chunk_size = self.buffer_size_seconds * sample_rate
        audio_start_time = time.time() - self.live_latency_seconds
        next_start_time = 0
        while not self.stop_event.is_set():
            chunk = process.stdout.read(4096)
            next_start_time = time.time() - self.live_latency_seconds  # Once we process the buffer, the current time is when the next buffer starts
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
            buffer = b''

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

    def create_process(
        self, info: StreamInfoObject
    ) -> tuple[subprocess.Popen[bytes] | None, int]:
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
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
            logger.debug(f"[{info.key}][MPEGFixedBitrateWorker][create_process] successfully created yt-dlp download process.")
            return process, sample_rate
        except FileNotFoundError:
            logger.error(f"[{info.key}][MPEGFixedBitrateWorker][create_process] 'yt-dlp' not found under '{self.ytdlp_path}'.")
        return process, sample_rate

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


class DASHWorker(AbstractWorker):
    """
    Worker that uses yt-dlp --live-from-start to download fragments of the stream.
    Since we are processing fragments, we can ensure perfect timestamps and higher quality (video+audio).
    
    This worker will:
    1. Start yt-dlp to download fragments to a tmp folder.
    2. Watch that folder for new fragments.
    3. Merge video and audio fragments.
    4. Process the merged fragments.
    5. Handle restarts by tracking state in a file.
    """

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][DASHWorker] Starting")
        
        # Setup paths
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        fragment_dir = os.path.join(project_root_dir, "tmp", info.key, "fragments")
        state_file = os.path.join(project_root_dir, "tmp", info.key, "dash_state.json")
        
        # Determine start time fallback
        try:
            initial_start_time = float(info.start_time)
        except (ValueError, TypeError):
            initial_start_time = time.time()
            logger.warning(f"[{info.key}][DASHWorker] Invalid info.start_time, defaulting to system time.")

        # Load state to handle resilience
        last_processed_seq, current_stream_time = self._load_state(state_file, info.stream_id, initial_start_time)

        # If we didn't recover a state (new stream or first run), perform cleanup of old fragments
        if last_processed_seq == 0 and current_stream_time == initial_start_time:
            logger.info(f"[{info.key}][DASHWorker] New stream detected or no state found. Cleaning up old fragments.")
            self._cleanup(fragment_dir)
            os.makedirs(fragment_dir, exist_ok=True)
        else:
            logger.info(f"[{info.key}][DASHWorker] Resuming from sequence {last_processed_seq} at time {current_stream_time}")
            os.makedirs(fragment_dir, exist_ok=True)

        # Start yt-dlp process
        process = self.create_process(info, fragment_dir)
        if process is None:
            logger.error(f"[{info.key}][DASHWorker] process failed to start.")
            return

        # Start monitoring loop
        self._monitor_loop(info, fragment_dir, state_file, process, last_processed_seq, current_stream_time)

        # Cleanup process when done
        if process.poll() is None:
             process.terminate()
        
        return

    def create_process(self, info: StreamInfoObject, fragment_dir: str) -> subprocess.Popen[bytes] | None:
        process = None
        logger.debug(f"[{info.key}][DASHWorker] creating yt-dlp download process")
        
        # Cleanup final file if it exists to prevent yt-dlp from skipping download
        try:
            files = glob.glob(os.path.join(fragment_dir, f"{info.stream_id}*"))
            for f in files:
                if os.path.isfile(f) and "Frag" not in f and not f.endswith(".part") and not f.endswith(".ytdl"):
                     try:
                         os.remove(f)
                         logger.info(f"[{info.key}][DASHWorker] Deleted existing final file {f} to prevent yt-dlp from skipping.")
                     except Exception as e:
                         logger.warning(f"[{info.key}][DASHWorker] Failed to delete file {f}: {e}")
        except Exception as e:
            logger.warning(f"[{info.key}][DASHWorker] Error during final file cleanup: {e}")

        try:
            # Output template to ensure filenames are predictable: id.format_id.FragmentNumber
            # FORCE H.264 (avc) and AAC (mp4a) to ensure MPEG-TS compatibility.
            # If we allow VP9, ffmpeg -c copy -f mpegts will drop the video track or create bin_data.
            cmd = [
                f"{self.ytdlp_path}",
                "--quiet",
                "--no-warnings",
                "--live-from-start",
                "--keep-fragments",
                "--match-filter", "is_live",
                "-f", "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
                "-o", f"{fragment_dir}/%(id)s.%(format_id)s",
                info.url,
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            logger.debug(f"[{info.key}][DASHWorker] successfully created yt-dlp download process.")
            return process
        except Exception as e:
            logger.error(f"[{info.key}][DASHWorker] failed to create process: {e}")
        return process

    def _cleanup(self, fragment_dir: str):
        if os.path.exists(fragment_dir):
            try:
                shutil.rmtree(fragment_dir)
            except Exception as e:
                logger.warning(f"[DASHWorker] failed to cleanup dir {fragment_dir}: {e}")

    def _get_chunk_duration(self, file_path: str) -> float:
        """
        Gets the duration of the media file using av directly. 
        """
        try:
            with av.open(file_path) as container:
                if container.duration:
                    return float(container.duration) / 1_000_000.0
                return 0.0
        except Exception as e:
            logger.error(f"[DASHWorker] Failed to get duration for {file_path}: {e}")
            return 0.0
        return 0.0
            
    def _is_complete_av(self, file_path: str) -> bool:
        """Checks if a file contains both video and audio streams."""
        try:
            with av.open(file_path) as container:
                has_video = len(container.streams.video) > 0
                has_audio = len(container.streams.audio) > 0
                return has_video and has_audio
        except Exception:
            return False
        return False

    def _load_state(self, state_path: str, current_stream_id: str, default_start_time: float) -> tuple[int, float]:
        """Loads the last processed sequence and current stream time from the state file."""
        if os.path.exists(state_path):
            try:
                with open(state_path, "r") as f:
                    data = json.load(f)
                    if data.get("stream_id") == current_stream_id:
                        return data.get("last_sequence", 0), data.get("current_stream_time", default_start_time)
            except Exception as e:
                logger.warning(f"[DASHWorker] Failed to load state: {e}")
        return 0, default_start_time

    def _save_state(self, state_path: str, stream_id: str, last_sequence: int, current_stream_time: float):
        """Saves the current state to the state file."""
        try:
            with open(state_path, "w") as f:
                json.dump({
                    "stream_id": stream_id,
                    "last_sequence": last_sequence,
                    "current_stream_time": current_stream_time
                }, f)
        except Exception as e:
            logger.warning(f"[DASHWorker] Failed to save state: {e}")

    def _monitor_loop(self, info: StreamInfoObject, fragment_dir: str, state_file: str, process: subprocess.Popen, start_seq: int, start_time: float):
        last_seq = start_seq
        current_stream_time = start_time
        
        buffer = bytearray()
        self.buffer_duration = 0.0

        logger.info(f"[{info.key}][DASHWorker] Monitoring {fragment_dir}")

        while not self.stop_event.is_set():
            if process.poll() is not None:
                logger.info(f"[{info.key}][DASHWorker] yt-dlp process ended.")
                break

            files = glob.glob(os.path.join(fragment_dir, "*"))
            
            # Filter valid fragment files
            valid_files = [f for f in files if "Frag" in f and not f.endswith(".part") and not f.endswith(".ytdl")]
            
            # Group files by sequence
            pending_fragments = {}
            for f_path in valid_files:
                filename = os.path.basename(f_path)
                match = re.search(r"Frag(\d+)", filename)
                if not match:
                    continue
                
                seq = int(match.group(1))
                
                # Resilience: Skip fragments we have already processed
                if seq <= last_seq:
                    continue

                if seq not in pending_fragments:
                    pending_fragments[seq] = []
                
                if os.path.getsize(f_path) == 0:
                    continue

                if f_path not in pending_fragments[seq]:
                    pending_fragments[seq].append(f_path)

            if not pending_fragments:
                 time.sleep(1)
                 continue
            
            # Process sequences in order
            sequences = sorted(pending_fragments.keys())
            for seq in sequences:
                files_for_seq = pending_fragments[seq]
                
                # Condition to process:
                # 1. We have at least 2 files (Assuming Video + Audio)
                # 2. OR We have 1 file AND it's a complete AV file (Video + Audio combined)
                # If NOT ready, break the loop to wait for this sequence to complete.
                
                is_ready = len(files_for_seq) >= 2
                is_single_complete = False
                
                if not is_ready and len(files_for_seq) == 1:
                     is_single_complete = self._is_complete_av(files_for_seq[0])

                if is_ready or is_single_complete:
                    merged_ts_path = os.path.join(fragment_dir, f"merged_{seq}.ts")
                    
                    if self._merge_fragments(files_for_seq, merged_ts_path):
                        # Calculate accurate duration using av
                        duration = self._get_chunk_duration(merged_ts_path)
                        
                        with open(merged_ts_path, "rb") as f:
                            data = f.read()

                        if duration > 0:
                            buffer.extend(data)
                            self.buffer_duration += duration
                        
                        # Cleanup merged file
                        try:
                            os.remove(merged_ts_path)
                        except OSError:
                            pass
                        
                        last_seq = seq
                        
                        # Check buffer size and emit
                        if self.buffer_duration >= self.buffer_size_seconds:
                            process_obj = ProcessObject(
                                raw=bytes(buffer),
                                audio_start_time=current_stream_time,
                                key=info.key,
                                # Force media_type to VIDEO since we are handling merged AV content
                                media_type=Media.VIDEO,
                            )
                            logger.debug(f"[{info.key}][DASHWorker] Adding chunk seq {seq} to queue. Duration: {self.buffer_duration:.2f}s")
                            self.queue.put(process_obj)
                            
                            current_stream_time += self.buffer_duration
                            buffer.clear()
                            self.buffer_duration = 0.0
                            
                            # Save state after successful queueing
                            self._save_state(state_file, info.stream_id, last_seq, current_stream_time)
                    else:
                        logger.error(f"[{info.key}][DASHWorker] Failed to merge fragments for seq {seq}")
                else:
                    # Sequence is incomplete. Stop processing to wait for it.
                    # This ensures we don't skip ahead.
                    break
            
            time.sleep(1)

        # Flush remaining buffer on stop
        if len(buffer) > 0:
             process_obj = ProcessObject(
                raw=bytes(buffer),
                audio_start_time=current_stream_time,
                key=info.key,
                media_type=info.media_type,
            )
             self.queue.put(process_obj)

    def _merge_fragments(self, inputs: list[str], output: str) -> bool:
        """Merges multiple input files into one MPEG-TS file using ffmpeg."""
        cmd = ["ffmpeg", "-y"]
        for inp in inputs:
            cmd.extend(["-i", inp])
        
        cmd.extend(["-c", "copy", "-f", "mpegts", output])
        
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except subprocess.CalledProcessError:
            return False
