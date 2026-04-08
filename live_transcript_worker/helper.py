import io
import json
import logging
import os
import re
import subprocess
import time

import av

from live_transcript_worker.config import Config
from live_transcript_worker.custom_types import Media, StreamInfoObject

logger = logging.getLogger(__name__)


class StreamHelper:
    @staticmethod
    def remove_date(title: str) -> str:
        """
        Given a title, this will remove the date and return the result.
        """
        pattern = r"\b(\d{4}-\d{2}-\d{2})\b|\b(\d{2}/\d{2}/\d{4})\b|\b(\d{2}:\d{2})\b"
        return re.sub(pattern, "", title).strip()

    @staticmethod
    def get_stream_stats(url: str) -> StreamInfoObject:
        """grabs the stats of a stream

        Note: yt-dlp -j is high cpu usage for whatever reason. This should only be called very infrequently.
        """
        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ytdlp_path = os.path.join(project_root_dir, "bin", "yt-dlp")
        cmd = [ytdlp_path, "-j", url]  # -j is alias for --dump-json
        process = None
        info = StreamInfoObject(url=url)
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            stdout, stderr = process.communicate(timeout=30)

            if process.returncode != 0:
                raise Exception(f"yt-dlp metadata fetch failed (code {process.returncode}): {stderr}")

            try:
                metadata: dict = json.loads(stdout)

                # Note
                # For YouTube, 'release_timestamp' is the epoch (s) for when the stream started
                # For Twitch, 'timestamp' is the epoch (s) for when the stream started
                info.is_live = metadata.get("is_live", False)
                if info.is_live:
                    info.stream_id = metadata.get("id", "Unknown ID")
                    info.stream_title = StreamHelper.remove_date(metadata.get("title", "Unknown Title"))
                    start_time = metadata.get("release_timestamp", 0)
                    if "twitch.tv" in url.lower():
                        info.stream_title = (
                            f"{metadata.get('display_id', 'Unknown Channel')} - {metadata.get('description', 'Unknown Title')}"
                        )
                        start_time = metadata.get("timestamp", 0)
                    if start_time == 0:
                        start_time = metadata.get("timestamp", time.time())
                        logger.warning("[stream_stats] start_time is still at 0")
                    info.start_time = str(start_time)

            except json.JSONDecodeError:
                logger.error("[stream_stats] Could not decode JSON metadata from yt-dlp.")
            except Exception as e:
                logger.error(f"[stream_stats] Error parsing metadata: {e}")

        except subprocess.TimeoutExpired:
            logger.error("[stream_stats] yt-dlp metadata fetch timed out.")
            if process:
                process.kill()
        except FileNotFoundError:
            logger.error("[stream_stats] 'yt-dlp' command not found for metadata fetch.")
        except Exception:
            # this is usually when it is a member stream, or the stream is not live yet.
            pass

        return info

    @staticmethod
    def get_stream_stats_until_valid_start(url: str, n: int) -> StreamInfoObject:
        info: StreamInfoObject = StreamHelper.get_stream_stats(url)

        if not info.is_live:
            return info

        while (info.start_time == "None" or info.start_time == "0" or info.start_time is None) and n > 0:
            logger.warning(f"[stream_stats_valid] start_time is not valid. type: {type(info.start_time)}, value: {info.start_time}, n: {n}")
            time.sleep(5)
            info = StreamHelper.get_stream_stats(url)
            n -= 1

            if not info.is_live:
                return info

        return info

    @staticmethod
    def get_precise_duration(data: bytes) -> float:
        """
        Calculates precise duration by decoding the audio stream and summing
        samples / sample_rate for every frame. More accurate than get_duration,
        which relies on container metadata that can drift over many segments.
        """
        try:
            with io.BytesIO(data) as buffer, av.open(buffer, mode="r") as container:
                # Priority 1: Decode audio — the master clock.
                if container.streams.audio:
                    audio_stream = container.streams.audio[0]
                    duration = 0.0
                    for frame in container.decode(audio_stream):
                        if frame.samples and frame.sample_rate:
                            duration += float(frame.samples) / float(frame.sample_rate)
                    return duration

                # Priority 2: Video stream metadata (no audio track).
                if container.streams.video:
                    video_stream = container.streams.video[0]
                    if video_stream.duration and video_stream.time_base:
                        return float(video_stream.duration * video_stream.time_base)

                # Priority 3: Container metadata (least accurate, last resort).
                if container.duration:
                    return float(container.duration) / 1_000_000.0

        except Exception as e:
            logger.error(f"[get_precise_duration] Failed: {e}")
        return 0.0

    @staticmethod
    def get_duration(audio: bytes):
        try:
            with io.BytesIO(audio) as buffer, av.open(buffer, mode="r") as container:
                duration_us = container.duration
                start_time_us = container.start_time

                if duration_us is None:
                    logger.warning(
                        "[audio_duration] Duration metadata not found in the stream.",
                    )
                    return 0.0

                # Convert duration from microseconds to seconds
                duration_sec = duration_us / 1_000_000.0
                start_time_sec = start_time_us / 1_000_000.0
                audio_duration = duration_sec - start_time_sec

                if audio_duration < 0:
                    return duration_sec

                return audio_duration
        except Exception:
            logger.error("[audio_duration] Invalid media data for buffer")
            return 0.0

        return 0.0

    @staticmethod
    def get_media_type(url: str, key: str) -> str:
        media_type = Media.NONE
        key_config = Config.get_streamer_config(key)
        if key_config:
            media_type = key_config.get("media_type", Media.NONE)

        return media_type
