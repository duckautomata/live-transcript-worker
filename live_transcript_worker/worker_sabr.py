import contextlib
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from queue import Queue

from live_transcript_worker.custom_types import ProcessObject, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.worker_abstract import AbstractWorker, StopEventLike

logger = logging.getLogger(__name__)


class SABRWorker(AbstractWorker):
    """
    Worker that uses yt-dlp's SABR disk-based downloader for live-from-start
    YouTube streams. Replaces DASHWorker (DASH is no longer served by YouTube).

    yt-dlp downloads the stream to disk as a pair of fMP4 fragment files per
    format:
      - {stream_id}.{format_id}.sqi.part   ← init segment (moov etc.)
      - {stream_id}.{format_id}.sq1.part   ← all media segments concatenated
      - {stream_id}.{format_id}.state      ← protobuf bookkeeping
    The .state file lets yt-dlp resume from the last completed segment after
    a process restart, and it embeds the YouTube broadcast_id so a stream-reset
    is detected automatically (yt-dlp wipes the .sq*.part on mismatch — we
    don't need DASHWorker's Frag1 byte-comparison verification).

    A background thread tail-reads .sqi.part + .sq1.part from byte 0 into an
    ffmpeg segmenter, which produces per-segment .ts files in `segments_dir`.
    The monitor loop consumes those segments exactly like DASHWorker consumed
    per-fragment files. Because the .sq1.part bytes are byte-identical across
    restarts (yt-dlp's resume preserves the file), ffmpeg's seg numbering is
    deterministic — segments with seq <= last_seq are skipped on resume, so
    we don't re-emit what was already queued.

    Audio-only by design — multi-format SABR would need named-pipe plumbing for
    a video+audio merge, which transcription doesn't need.
    """

    SEGMENT_DURATION = 6
    # Time to wait between tail-reader poll iterations.
    TAIL_POLL_INTERVAL = 0.5
    # Time to wait for yt-dlp to write its first init/.state file before giving up.
    INIT_TIMEOUT_SECONDS = 60

    def __init__(self, key: str, queue: Queue, stop_event: StopEventLike):
        super().__init__(key, queue, stop_event)
        # Override the default yt-dlp path with the SABR fork. Upstream yt-dlp
        # doesn't ship the SABR downloader yet — only duckautomata/yt-dlp-sabr does.
        # See scripts/setup.sh, which fetches both binaries.
        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.ytdlp_path = os.path.join(project_root_dir, "bin", "yt-dlp-sabr")

    def start(self, info: StreamInfoObject):
        logger.info(f"[{info.key}][SABRWorker] Starting")

        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # yt-dlp writes its files here (.sqi.part / .sq*.part / .state).
        fragment_dir = os.path.join(project_root_dir, "tmp", info.key, "fragments")
        # ffmpeg writes its segmented .ts output here, separated so the two file
        # name spaces don't collide and our scan regex stays simple.
        segments_dir = os.path.join(project_root_dir, "tmp", info.key, "segments")
        state_file = os.path.join(project_root_dir, "tmp", info.key, "sabr_state.json")

        try:
            initial_start_time = float(info.start_time)
        except (ValueError, TypeError):
            initial_start_time = time.time()
            logger.warning(f"[{info.key}][SABRWorker] Invalid info.start_time, defaulting to system time.")

        last_processed_seq, current_stream_time = self._load_state(state_file, info.stream_id, initial_start_time)

        if last_processed_seq == 0 and current_stream_time == initial_start_time:
            logger.info(f"[{info.key}][SABRWorker] New stream or no state. Cleaning up old fragments.")
            self._cleanup_dir(info, fragment_dir)
            self._cleanup_dir(info, segments_dir)
        else:
            logger.info(
                f"[{info.key}][SABRWorker] Resuming from seq {last_processed_seq} at stream time {current_stream_time}"
            )
            # ffmpeg's seq numbering restarts from 1 each run. We rebuild deterministic
            # output by feeding the .sq1.part from byte 0, so wipe segments_dir to
            # avoid mixing prior-run segments (which might have different durations
            # if yt-dlp truncated) with the fresh run's output.
            self._cleanup_dir(info, segments_dir)

        os.makedirs(fragment_dir, exist_ok=True)
        os.makedirs(segments_dir, exist_ok=True)

        ytdlp_proc = self._create_ytdlp_process(info, fragment_dir)
        if ytdlp_proc is None:
            logger.error(f"[{info.key}][SABRWorker] yt-dlp failed to start.")
            return

        ffmpeg_proc = self._create_ffmpeg_process(info, segments_dir)
        if ffmpeg_proc is None:
            logger.error(f"[{info.key}][SABRWorker] ffmpeg failed to start.")
            with contextlib.suppress(Exception):
                ytdlp_proc.terminate()
            return

        tail_stop = threading.Event()
        tail_thread = threading.Thread(
            target=self._tail_reader,
            args=(info, fragment_dir, ffmpeg_proc, ytdlp_proc, tail_stop),
            daemon=True,
        )
        tail_thread.start()

        try:
            last_processed_seq, current_stream_time, broadcast_reset = self._monitor_loop(
                info,
                segments_dir,
                state_file,
                ytdlp_proc,
                ffmpeg_proc,
                last_processed_seq,
                current_stream_time,
            )
        finally:
            tail_stop.set()
            for proc in (ffmpeg_proc, ytdlp_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            tail_thread.join(timeout=5)

        # If yt-dlp exited cleanly without us advancing past last_processed_seq,
        # the broadcast probably reset (yt-dlp wiped its .sq1.part on broadcast_id
        # mismatch). Clear our state so the next worker start treats it as new.
        if broadcast_reset:
            logger.info(f"[{info.key}][SABRWorker] Suspected broadcast reset; clearing state for clean restart.")
            with contextlib.suppress(OSError):
                os.remove(state_file)

    # ------------------------------------------------------------------
    # yt-dlp / ffmpeg subprocess setup
    # ------------------------------------------------------------------

    def _create_ytdlp_process(self, info: StreamInfoObject, fragment_dir: str) -> subprocess.Popen[bytes] | None:
        """Spawn yt-dlp in disk-based SABR mode with --keep-fragments + --continue."""
        logger.debug(f"[{info.key}][SABRWorker] creating yt-dlp download process")

        # Remove any non-fragment leftover (e.g. final muxed file from a prior run)
        # that would cause yt-dlp to skip the download. .sqi.part/.sq*.part/.state
        # files are kept so resume works.
        try:
            for f in glob.glob(os.path.join(fragment_dir, f"{info.stream_id}*")):
                if os.path.isfile(f) and not (
                    f.endswith(".sqi.part") or re.search(r"\.sq\d+\.part$", f) or f.endswith(".state")
                ):
                    with contextlib.suppress(OSError):
                        os.remove(f)
                        logger.info(f"[{info.key}][SABRWorker] Removed leftover {f} so yt-dlp won't skip download.")
        except Exception as e:
            logger.warning(f"[{info.key}][SABRWorker] Pre-start cleanup error: {e}")

        # Audio-only — SABR multi-format would require named-pipe plumbing into ffmpeg's
        # stdin, which transcription doesn't need.
        fmt_selector = "bestaudio[acodec^=mp4a]/ba/best"

        cmd = [
            f"{self.ytdlp_path}",
            "--live-from-start",
            "--keep-fragments",
            "--continue",
            "--no-progress",
            "--no-colors",
            *StreamHelper.ytdlp_auth_args(info.url, purpose="download"),
            "--retries",
            "20",
            "--fragment-retries",
            "10",
            "--extractor-retries",
            "5",
            "--socket-timeout",
            "30",
            "--retry-sleep",
            "fragment:exp=1:10",
            "--retry-sleep",
            "http:exp=1:60",
            "--retry-sleep",
            "extractor:exp=1:60",
            # SABR-level reconnect budget (independent of --fragment-retries which is per-segment).
            "--extractor-args",
            "youtube:sabr_stream_retries=30",
            "-f",
            fmt_selector,
            "-o",
            f"{fragment_dir}/%(id)s.%(format_id)s",
            info.url,
        ]

        log_path = os.path.join(os.path.dirname(fragment_dir), "ytdlp.log")
        try:
            with open(log_path, "a") as log_file:
                log_file.write(f"\n--- yt-dlp started at {time.strftime('%Y-%m-%d %H:%M:%S')} (stream: {info.stream_id}) ---\n")
                log_file.flush()
                process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
            logger.debug(f"[{info.key}][SABRWorker] yt-dlp pid={process.pid}, logging to {log_path}")
            return process
        except Exception as e:
            logger.error(f"[{info.key}][SABRWorker] failed to create yt-dlp process: {e}")
            return None

    def _create_ffmpeg_process(self, info: StreamInfoObject, segments_dir: str) -> subprocess.Popen[bytes] | None:
        """Spawn ffmpeg as a streaming segmenter reading from stdin."""
        log_path = os.path.join(os.path.dirname(segments_dir), "ffmpeg_sabr.log")
        # Output names are processed by the monitor regex `seg_(\d+)\.ts`.
        segment_pattern = os.path.join(segments_dir, "seg_%d.ts")

        cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.SEGMENT_DURATION),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            "-segment_start_number",
            "1",
            segment_pattern,
        ]

        try:
            with open(log_path, "ab") as log_file:
                log_file.write(
                    f"\n--- ffmpeg started at {time.strftime('%Y-%m-%d %H:%M:%S')} (stream: {info.stream_id}) ---\n".encode()
                )
                log_file.flush()
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    bufsize=0,
                )
            logger.debug(f"[{info.key}][SABRWorker] ffmpeg pid={process.pid}, logging to {log_path}")
            return process
        except Exception as e:
            logger.error(f"[{info.key}][SABRWorker] failed to create ffmpeg process: {e}")
            return None

    # ------------------------------------------------------------------
    # Tail reader thread
    # ------------------------------------------------------------------

    def _find_format_files(self, fragment_dir: str, stream_id: str) -> tuple[str, str] | None:
        """Locate the (.sqi.part, .sq1.part) pair for the active format.

        Format ID isn't known until yt-dlp writes the files (we let yt-dlp pick AAC
        via the fmt selector but the numeric ID can vary). Returns (init, sequence)
        paths, or None if either is missing yet.
        """
        sqi_matches = sorted(glob.glob(os.path.join(fragment_dir, f"{stream_id}.*.sqi.part")))
        if not sqi_matches:
            return None
        sqi_path = sqi_matches[0]
        # Strip the `.sqi.part` suffix to recover the format prefix, then look for sq1.
        prefix = sqi_path[: -len(".sqi.part")]
        sq1_path = f"{prefix}.sq1.part"
        if not os.path.exists(sq1_path):
            return None
        return sqi_path, sq1_path

    def _tail_reader(
        self,
        info: StreamInfoObject,
        fragment_dir: str,
        ffmpeg_proc: subprocess.Popen,
        ytdlp_proc: subprocess.Popen,
        stop_event: threading.Event,
    ):
        """Read init segment + sequence file (tailing as it grows), pipe to ffmpeg stdin.

        Waits for the .sqi.part / .sq1.part pair to appear (yt-dlp writes them after
        contacting YouTube), then streams init bytes followed by the sequence file
        content from byte 0 onward. Continues as long as the file grows; sleeps when
        no new bytes are available. Exits when stop_event is set, ffmpeg's stdin pipe
        breaks, or yt-dlp finishes and the file stops growing.
        """
        ffmpeg_stdin = ffmpeg_proc.stdin
        if ffmpeg_stdin is None:
            logger.error(f"[{info.key}][SABRWorker][tail] ffmpeg stdin not available; aborting.")
            return

        # Wait for yt-dlp to produce the init + sequence files.
        wait_start = time.time()
        paths = None
        while not stop_event.is_set():
            paths = self._find_format_files(fragment_dir, info.stream_id)
            if paths is not None:
                break
            if ytdlp_proc.poll() is not None:
                logger.warning(
                    f"[{info.key}][SABRWorker][tail] yt-dlp exited (rc={ytdlp_proc.returncode}) "
                    f"before producing files; aborting tail reader."
                )
                with contextlib.suppress(Exception):
                    ffmpeg_stdin.close()
                return
            if time.time() - wait_start > self.INIT_TIMEOUT_SECONDS:
                logger.warning(f"[{info.key}][SABRWorker][tail] Timed out waiting for init/sequence files.")
                with contextlib.suppress(Exception):
                    ffmpeg_stdin.close()
                return
            time.sleep(self.TAIL_POLL_INTERVAL)

        if paths is None:
            return
        sqi_path, sq1_path = paths
        logger.debug(f"[{info.key}][SABRWorker][tail] feeding {os.path.basename(sqi_path)} + {os.path.basename(sq1_path)}")

        try:
            with open(sqi_path, "rb") as f:
                init_data = f.read()
            if init_data:
                ffmpeg_stdin.write(init_data)
                ffmpeg_stdin.flush()
        except (OSError, BrokenPipeError) as e:
            logger.warning(f"[{info.key}][SABRWorker][tail] init read/write failed: {e}")
            with contextlib.suppress(Exception):
                ffmpeg_stdin.close()
            return

        offset = 0
        # Stream content from .sq1.part. Always feed from byte 0 — the file is
        # byte-identical across yt-dlp restarts (resume preserves it), so feeding
        # from 0 is what makes ffmpeg's seq numbering deterministic across restarts.
        try:
            with open(sq1_path, "rb") as sq_fp:
                while not stop_event.is_set():
                    try:
                        current_size = os.path.getsize(sq1_path)
                    except OSError:
                        break

                    if current_size > offset:
                        sq_fp.seek(offset)
                        chunk = sq_fp.read(current_size - offset)
                        if not chunk:
                            time.sleep(self.TAIL_POLL_INTERVAL)
                            continue
                        try:
                            ffmpeg_stdin.write(chunk)
                            ffmpeg_stdin.flush()
                        except (BrokenPipeError, OSError) as e:
                            logger.info(f"[{info.key}][SABRWorker][tail] ffmpeg pipe closed: {e}")
                            break
                        offset += len(chunk)
                    else:
                        # No new bytes. If yt-dlp has exited, drain and stop.
                        if ytdlp_proc.poll() is not None:
                            logger.info(
                                f"[{info.key}][SABRWorker][tail] yt-dlp exited (rc={ytdlp_proc.returncode}) "
                                f"and file stopped growing at {offset} bytes; closing pipe."
                            )
                            break
                        time.sleep(self.TAIL_POLL_INTERVAL)
        except OSError as e:
            logger.warning(f"[{info.key}][SABRWorker][tail] sequence read failed: {e}")
        finally:
            with contextlib.suppress(Exception):
                ffmpeg_stdin.close()

    # ------------------------------------------------------------------
    # Monitor loop (consumes ffmpeg's .ts segments)
    # ------------------------------------------------------------------

    def _monitor_loop(
        self,
        info: StreamInfoObject,
        segments_dir: str,
        state_file: str,
        ytdlp_proc: subprocess.Popen,
        ffmpeg_proc: subprocess.Popen,
        start_seq: int,
        start_time: float,
    ) -> tuple[int, float, bool]:
        """Watches segments_dir for completed .ts files from ffmpeg, processes them.

        Returns (last_seq, current_stream_time, broadcast_reset). `broadcast_reset`
        is True if we suspect yt-dlp wiped its state due to a broadcast_id mismatch
        — caller will clear our state file in that case.
        """
        last_seq = start_seq
        current_stream_time = start_time
        buffer = bytearray()
        buffer_duration = 0.0
        last_new_segment_time = time.time()
        seg_re = re.compile(r"^seg_(\d+)\.ts$")

        # If we resumed at last_seq=N, ffmpeg will emit seg_1.ts..seg_N.ts identical
        # to the prior run (from the same byte-identical .sq1.part input). We delete
        # those without queueing. If after the staleness timeout we still haven't
        # advanced past start_seq, that's the signal that yt-dlp wiped its state for
        # a broadcast_id mismatch — in that case we'd never re-reach start_seq and
        # we tell the caller to clear our worker state.
        broadcast_reset = False

        logger.info(f"[{info.key}][SABRWorker] Monitoring {segments_dir} (resume from seq {start_seq})")

        while not self.stop_event.is_set():
            ytdlp_alive = ytdlp_proc.poll() is None
            ffmpeg_alive = ffmpeg_proc.poll() is None

            if not ytdlp_alive or not ffmpeg_alive:
                if ytdlp_proc.returncode and ytdlp_proc.returncode != 0 and last_seq == start_seq == 0:
                    logger.warning(
                        f"[{info.key}][SABRWorker] yt-dlp exited with code {ytdlp_proc.returncode} "
                        f"before producing any segments. Switching to LiveSegmentWorker."
                    )
                    self.is_slow = True
                else:
                    logger.info(
                        f"[{info.key}][SABRWorker] pipeline exited "
                        f"(yt-dlp={ytdlp_proc.returncode}, ffmpeg={ffmpeg_proc.returncode})."
                    )
                # Drain any final segments that ffmpeg flushed at exit before we leave.
                # Only do this once both children are gone so we know no more segments
                # are coming and the highest one is fully written.
                if not ytdlp_alive and not ffmpeg_alive:
                    last_seq, current_stream_time, _ = self._drain_segments(
                        info, segments_dir, state_file, seg_re, buffer, buffer_duration, last_seq, current_stream_time
                    )
                break

            try:
                names = os.listdir(segments_dir)
            except FileNotFoundError:
                break

            pending: list[tuple[int, str]] = []
            for name in names:
                m = seg_re.match(name)
                if not m:
                    continue
                seq = int(m.group(1))
                full = os.path.join(segments_dir, name)
                if seq <= last_seq:
                    # Already-processed seg (resume case) or replayed seg from a
                    # broadcast_id-reset run. Either way, drop it.
                    with contextlib.suppress(OSError):
                        os.remove(full)
                    continue
                try:
                    if os.path.getsize(full) == 0:
                        continue
                except OSError:
                    continue
                pending.append((seq, full))

            if not pending:
                # Stall watchdog: same shape as DASHWorker. Distinguishes "stream
                # ended" from "broadcast reset and we'll never reach start_seq".
                if time.time() - last_new_segment_time > self.stale_ytdlp_seconds:
                    if last_seq == start_seq and start_seq > 0:
                        # We resumed expecting seq>start_seq but never got there —
                        # most likely yt-dlp wiped its state on a broadcast_id mismatch.
                        logger.warning(
                            f"[{info.key}][SABRWorker] Resume stalled at seq {last_seq}; suspect broadcast reset. "
                            f"Terminating pipeline so caller can clear state."
                        )
                        broadcast_reset = True
                    else:
                        logger.warning(
                            f"[{info.key}][SABRWorker] No new segments in {time.time() - last_new_segment_time:.1f}s "
                            f"(yt-dlp pid={ytdlp_proc.pid}). Terminating pipeline."
                        )
                    break
                time.sleep(1)
                continue

            last_new_segment_time = time.time()
            pending.sort()

            # Skip the highest-numbered segment — ffmpeg is currently writing it.
            # We pick it up next iteration once ffmpeg rolls to the next segment.
            if len(pending) < 2:
                time.sleep(1)
                continue
            consumable = pending[:-1]

            for seq, path in consumable:
                if self.stop_event.is_set():
                    logger.info(f"[{info.key}][SABRWorker] stop event set, exiting.")
                    break

                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError as e:
                    logger.warning(f"[{info.key}][SABRWorker] read failed for {path}: {e}")
                    continue

                duration = StreamHelper.get_precise_duration(data)
                if duration > 0:
                    buffer.extend(data)
                    buffer_duration += duration

                with contextlib.suppress(OSError):
                    os.remove(path)

                last_seq = seq

                if buffer_duration >= self.buffer_size_seconds - 0.2:
                    process_obj = ProcessObject(
                        raw=bytes(buffer),
                        audio_start_time=current_stream_time,
                        key=info.key,
                        media_type=info.media_type,
                        vod_accurate=True,
                    )
                    logger.debug(
                        f"[{info.key}][SABRWorker] Adding chunk seq {seq} to queue. Duration: {buffer_duration:.3f}s"
                    )
                    self.queue.put(process_obj)

                    current_stream_time += buffer_duration
                    buffer.clear()
                    buffer_duration = 0.0

                    self._save_state(state_file, info.stream_id, last_seq, current_stream_time)

                    gap = time.time() - current_stream_time
                    if gap > self.stale_lfs_gap_seconds:
                        logger.warning(
                            f"[{info.key}][SABRWorker] Worker is {gap / 60:.1f} minutes behind live "
                            f"(threshold: {self.stale_lfs_gap_seconds / 60:.1f} min). Switching to LiveSegmentWorker."
                        )
                        self.is_slow = True
                        return last_seq, current_stream_time, broadcast_reset

            time.sleep(1)

        # Flush remaining buffer on stop
        if len(buffer) > 0:
            process_obj = ProcessObject(
                raw=bytes(buffer),
                audio_start_time=current_stream_time,
                key=info.key,
                media_type=info.media_type,
                vod_accurate=True,
            )
            self.queue.put(process_obj)

            current_stream_time += buffer_duration
            self._save_state(state_file, info.stream_id, last_seq, current_stream_time)

        return last_seq, current_stream_time, broadcast_reset

    def _drain_segments(
        self,
        info: StreamInfoObject,
        segments_dir: str,
        state_file: str,
        seg_re: re.Pattern,
        buffer: bytearray,
        buffer_duration: float,
        last_seq: int,
        current_stream_time: float,
    ) -> tuple[int, float, float]:
        """Consume any complete .ts segments left in segments_dir after the pipeline
        exits. Now that ffmpeg is gone, the highest-numbered file is also complete,
        so we don't skip it like in the live monitor loop."""
        try:
            names = os.listdir(segments_dir)
        except FileNotFoundError:
            return last_seq, current_stream_time, buffer_duration

        pending: list[tuple[int, str]] = []
        for name in names:
            m = seg_re.match(name)
            if not m:
                continue
            seq = int(m.group(1))
            full = os.path.join(segments_dir, name)
            if seq <= last_seq:
                with contextlib.suppress(OSError):
                    os.remove(full)
                continue
            pending.append((seq, full))

        if not pending:
            return last_seq, current_stream_time, buffer_duration
        pending.sort()

        for seq, path in pending:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            duration = StreamHelper.get_precise_duration(data)
            if duration > 0:
                buffer.extend(data)
                buffer_duration += duration
            with contextlib.suppress(OSError):
                os.remove(path)
            last_seq = seq

            if buffer_duration >= self.buffer_size_seconds - 0.2:
                self.queue.put(
                    ProcessObject(
                        raw=bytes(buffer),
                        audio_start_time=current_stream_time,
                        key=info.key,
                        media_type=info.media_type,
                        vod_accurate=True,
                    )
                )
                current_stream_time += buffer_duration
                buffer.clear()
                buffer_duration = 0.0
                self._save_state(state_file, info.stream_id, last_seq, current_stream_time)

        return last_seq, current_stream_time, buffer_duration

    # ------------------------------------------------------------------
    # State + cleanup
    # ------------------------------------------------------------------

    def _cleanup_dir(self, info: StreamInfoObject, dir_path: str):
        if not os.path.exists(dir_path):
            return
        try:
            shutil.rmtree(dir_path)
        except Exception as e:
            logger.warning(f"[{info.key}][SABRWorker] Failed to cleanup dir {dir_path}: {e}")

    def _load_state(self, state_path: str, current_stream_id: str, default_start_time: float) -> tuple[int, float]:
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    data = json.load(f)
                    if data.get("stream_id") == current_stream_id:
                        return data.get("last_sequence", 0), data.get("current_stream_time", default_start_time)
            except Exception as e:
                logger.warning(f"[SABRWorker] Failed to load state: {e}")
        return 0, default_start_time

    def _save_state(
        self,
        state_path: str,
        stream_id: str,
        last_sequence: int,
        current_stream_time: float,
    ):
        try:
            dir_name = os.path.dirname(state_path)
            os.makedirs(dir_name, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(
                        {
                            "stream_id": stream_id,
                            "last_sequence": last_sequence,
                            "current_stream_time": current_stream_time,
                        },
                        f,
                    )
                os.replace(tmp_path, state_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning(f"[SABRWorker] Failed to save state: {e}")
