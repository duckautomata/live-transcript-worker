import logging
import os
import shutil
import glob
import re
import json
import av
import subprocess
import time
from src.live_transcript_worker.worker_abstract import AbstractWorker

from src.live_transcript_worker.types import Media, ProcessObject, StreamInfoObject

logger = logging.getLogger(__name__)

class DASHWorker(AbstractWorker):
    """
    Worker that uses yt-dlp --live-from-start to download fragments of the stream.
    Since we are processing fragments, we can ensure perfect timestamps and higher quality (video+audio).
    
    This worker will:
    1. Start yt-dlp to download fragments to a tmp folder.
    2. Watch that folder for new fragments.
    3. Merge video and audio fragments (or just process audio).
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
            
            # Determine format selector based on media_type
            if info.media_type == Media.VIDEO:
                # FORCE H.264 (avc) and AAC (mp4a) to ensure MPEG-TS compatibility.
                # If we allow VP9, ffmpeg -c copy -f mpegts will drop the video track or create bin_data.
                fmt_selector = "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best"
            else:
                # Audio only (Media.AUDIO or Media.NONE)
                fmt_selector = "bestaudio/best"

            cmd = [
                f"{self.ytdlp_path}",
                "--quiet",
                "--no-warnings",
                "--live-from-start",
                "--keep-fragments",
                "--match-filter", "is_live",
                "-f", fmt_selector,
                "-o", f"{fragment_dir}/%(id)s.%(format_id)s",
                info.url,
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            logger.debug(f"[{info.key}][DASHWorker] successfully created yt-dlp download process with mode {info.media_type}.")
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
        Calculates the precise duration based on the Audio Stream.
        """
        try:
            with av.open(file_path) as container:
                # Priority 1: Decode Audio (The "Master Clock")
                if container.streams.audio:
                    audio_stream = container.streams.audio[0]
                    duration = 0.0
                    
                    # Decode every frame to get exact sample count.
                    # This is fast because we are not converting/resampling, just counting.
                    for frame in container.decode(audio_stream):
                        if frame.samples and frame.sample_rate:
                            duration += float(frame.samples) / float(frame.sample_rate)
                    
                    return duration

                # Priority 2: Video Stream Duration (Fallback if no audio)
                elif container.streams.video:
                    # If audio is missing, we fall back to video duration to keep the timeline moving
                    video_stream = container.streams.video[0]
                    if video_stream.duration and video_stream.time_base:
                        return float(video_stream.duration * video_stream.time_base)

                # Priority 3: Container Metadata (Least accurate, last resort)
                elif container.duration:
                    return float(container.duration) / 1_000_000.0
                
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
        
        # The chunk of fragments. duration is used to keep track of how large the chunk is.
        buffer = bytearray()
        buffer_duration = 0.0

        # Determine if we are in video mode
        is_video_mode = info.media_type == Media.VIDEO

        logger.info(f"[{info.key}][DASHWorker] Monitoring {fragment_dir} (Mode: {info.media_type})")

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
                
                is_ready = False
                
                if is_video_mode:
                    # Video Mode Condition:
                    # 1. At least 2 files (Video + Audio)
                    # 2. OR 1 file AND it's a complete AV file
                    is_ready = len(files_for_seq) >= 2
                    if not is_ready and len(files_for_seq) == 1:
                         is_ready = self._is_complete_av(files_for_seq[0])
                else:
                    # Audio Mode Condition:
                    # Just need 1 file (the audio track)
                    is_ready = len(files_for_seq) >= 1

                if is_ready:
                    merged_ts_path = os.path.join(fragment_dir, f"merged_{seq}.ts")
                    
                    # Even for audio-only, we run it through merge_fragments (ffmpeg -c copy -f mpegts).
                    # This standardizes the container to MPEG-TS for downstream processing.
                    if self._merge_fragments(files_for_seq, merged_ts_path):
                        # Calculate accurate duration using av
                        duration = self._get_chunk_duration(merged_ts_path)
                        
                        with open(merged_ts_path, "rb") as f:
                            data = f.read()

                        if duration > 0:
                            buffer.extend(data)
                            buffer_duration += duration
                        
                        # Cleanup merged file
                        try:
                            os.remove(merged_ts_path)
                        except OSError:
                            pass
                        
                        last_seq = seq
                        
                        # Check buffer size and emit. We subtract 200ms since the fragments aren't exact. And we want to process a chunk that is 5.99 seconds since it is close enough to 6.
                        if buffer_duration >= self.buffer_size_seconds - 0.2:
                            process_obj = ProcessObject(
                                raw=bytes(buffer),
                                audio_start_time=current_stream_time,
                                key=info.key,
                                media_type=info.media_type,
                            )
                            logger.debug(f"[{info.key}][DASHWorker] Adding chunk seq {seq} to queue. Duration: {buffer_duration:.3f}s")
                            self.queue.put(process_obj)
                            
                            current_stream_time += buffer_duration
                            buffer.clear()
                            buffer_duration = 0.0
                            
                            # Save state after successful queueing
                            self._save_state(state_file, info.stream_id, last_seq, current_stream_time)
                    else:
                        logger.error(f"[{info.key}][DASHWorker] Failed to process fragments for seq {seq}")
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
