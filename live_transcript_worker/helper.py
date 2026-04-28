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
    def ytdlp_auth_args(url: str) -> list[str]:
        """
        Returns YouTube-only yt-dlp args for authentication and content filtering:
        - --cookies <file> when `server.cookies.enabled` is true (bypasses bot restrictions)
        - --match-filter to skip members-only content (YouTube subscriber_only)

        Returns an empty list for non-YouTube URLs (e.g. Twitch), since Twitch doesn't
        expose an `availability` field and doesn't need the cookies.
        """
        if "twitch.tv" in url.lower():
            return []

        args = ["--match-filter", "availability!=?subscriber_only"]

        cookies_cfg: dict = Config.get_server_config().get("cookies", {}) or {}
        if cookies_cfg.get("enabled", False):
            filename = cookies_cfg.get("filename", "cookies.txt")
            project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cookies_path = os.path.join(project_root_dir, filename)
            if os.path.isfile(cookies_path):
                args.extend(["--cookies", cookies_path])
            else:
                logger.warning(f"[ytdlp_auth_args] Cookies enabled but file not found at '{cookies_path}'.")
        return args

    @staticmethod
    def remove_date(title: str) -> str:
        """
        Given a title, this will remove the date and return the result.
        """
        pattern = r"\b(\d{4}-\d{2}-\d{2})\b|\b(\d{2}/\d{2}/\d{4})\b|\b(\d{2}:\d{2})\b"
        return re.sub(pattern, "", title).strip()

    @staticmethod
    def _dump_stream_stats_debug(key: str, url: str, returncode: int | None, stdout: str, stderr: str) -> None:
        """Writes the raw yt-dlp metadata response (or error output) to tmp/{key}/stream_stats.log
        for debugging. Overwritten on each call so each key has its own latest response."""
        if not key:
            return
        try:
            project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            debug_dir = os.path.join(project_root_dir, "tmp", key)
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, "stream_stats.log")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(f"--- yt-dlp -j response at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(f"url: {url}\n")
                f.write(f"returncode: {returncode}\n")
                f.write("--- stdout ---\n")
                f.write(stdout or "")
                f.write("\n--- stderr ---\n")
                f.write(stderr or "")
        except Exception as e:
            logger.warning(f"[stream_stats] Failed to write debug file: {e}")

    @staticmethod
    def _parse_upcoming_seconds(stderr: str) -> int | None:
        """Parses 'This live event will begin in <duration>.' from yt-dlp stderr.
        Handles 'X days', 'X hours, Y minutes', etc. Returns total seconds, or None."""
        match = re.search(r"will begin in ([^.]+)", stderr)
        if not match:
            return None
        units = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}
        seconds = sum(int(v) * units[u] for v, u in re.findall(r"(\d+)\s+(day|hour|minute|second)s?", match.group(1)))
        return seconds or None

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Render seconds as '2 hours, 11 minutes, 30 seconds'. Drops zero-valued
        units and pluralizes correctly. Returns '0 seconds' for non-positive input."""
        s = int(seconds)
        if s <= 0:
            return "0 seconds"
        parts = []
        for name, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
            n, s = divmod(s, size)
            if n:
                parts.append(f"{n} {name}{'s' if n != 1 else ''}")
        return ", ".join(parts)

    @staticmethod
    def get_stream_stats(url: str, key: str = "") -> StreamInfoObject:
        """grabs the stats of a stream

        Note: yt-dlp -j is high cpu usage for whatever reason. This should only be called very infrequently.
        """
        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ytdlp_path = os.path.join(project_root_dir, "bin", "yt-dlp")
        cmd = [ytdlp_path, "-j", *StreamHelper.ytdlp_auth_args(url), url]  # -j is alias for --dump-json
        process = None
        info = StreamInfoObject(url=url)
        stdout = ""
        stderr = ""
        returncode: int | None = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            stdout, stderr = process.communicate(timeout=30)
            returncode = process.returncode

            if returncode != 0 and "twitch.tv" not in url.lower():
                # yt-dlp errors for upcoming or offline YouTube channels with informative
                # stderr. Parse it so the watcher can back off polling.
                # Skipped for Twitch since it has no scheduled-start concept.
                upcoming = StreamHelper._parse_upcoming_seconds(stderr)
                if upcoming is not None:
                    info.scheduled_start_time = time.time() + upcoming
                elif "not currently live" in stderr:
                    info.confirmed_offline = True
            else:
                try:
                    metadata: dict = json.loads(stdout)

                    # For YouTube, 'release_timestamp' is the stream start epoch.
                    # For Twitch, 'timestamp' is the stream start epoch.
                    info.is_live = metadata.get("is_live", False) or metadata.get("live_status", "") == "is_live"
                    if info.is_live:
                        info.stream_id = metadata.get("id", "Unknown ID")
                        if "twitch.tv" in url.lower():
                            info.stream_title = (
                                f"{metadata.get('display_id', 'Unknown Channel')} - {metadata.get('description', 'Unknown Title')}"
                            )
                            start_time = metadata.get("timestamp")
                        else:
                            info.stream_title = StreamHelper.remove_date(metadata.get("title", "Unknown Title"))
                            start_time = metadata.get("release_timestamp") or metadata.get("timestamp")

                        if not start_time:
                            logger.warning(f"[stream_stats] Could not find start_time for {url}. Falling back to current time.")
                            start_time = time.time()
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
            pass

        StreamHelper._dump_stream_stats_debug(key, url, returncode, stdout, stderr)
        return info

    @staticmethod
    def get_stream_stats_until_valid_start(url: str, n: int, key: str = "") -> StreamInfoObject:
        info: StreamInfoObject = StreamHelper.get_stream_stats(url, key)

        if not info.is_live:
            return info

        while (info.start_time in ["None", "0", "0.0"] or info.start_time is None) and n > 0:
            logger.warning(f"[stream_stats_valid] start_time is not valid. type: {type(info.start_time)}, value: {info.start_time}, n: {n}")
            time.sleep(5)
            info = StreamHelper.get_stream_stats(url, key)
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
