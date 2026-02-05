import logging
import os
import threading
from typing import List

import httpx

from src.live_transcript_worker.config import Config

logger = logging.getLogger(__name__)


class StatusReporter(threading.Thread):
    def __init__(self, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        self.server_config = Config.get_server_config()
        self.enable_request = self.server_config.get("enabled", False)
        self.base_url = self.server_config.get("url", "http://localhost:8080")
        api_key = self.server_config.get("apiKey", "")
        self.headers = {"X-API-Key": api_key.strip()}

        # Initialize persistent client
        self.client = httpx.Client(base_url=self.base_url, headers=self.headers, timeout=10.0)

    def run(self):
        if self.enable_request is False:
            logger.info("[StatusReporter] Request disabled - skipping status reporter.")
            return
        logger.info("[StatusReporter] Starting status reporter thread")
        while not self.stop_event.is_set():
            try:
                self.send_status()
            except Exception as e:
                logger.error(f"[StatusReporter] Error sending status: {e}")

            # Wait for 60 seconds or until stop event is set
            if self.stop_event.wait(60):
                break

        logger.info("[StatusReporter] Stopping status reporter thread")

    def send_status(self):
        version = os.getenv("APP_VERSION", "local")
        build_time = os.getenv("BUILD_DATE", "unknown")

        streamers = Config.get_all_streamers_config()
        keys: List[str] = [s.get("key") for s in streamers if s.get("key")]  # type: ignore

        payload = {"version": version, "build_time": build_time, "keys": keys}

        try:
            response = self.client.post("/status", json=payload)
            if response.status_code != 200:
                logger.warning(f"[StatusReporter] Server returned {response.status_code}: {response.text}")
        except httpx.RequestError as e:
            logger.error(f"[StatusReporter] Network error sending status: {e}")
