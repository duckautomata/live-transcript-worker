import logging
import marshal
import os
import queue
import shutil
import threading
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from src.live_transcript_worker.config import Config
from src.live_transcript_worker.custom_types import MediaUploadObject, StreamInfoObject

logger = logging.getLogger(__name__)


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class Storage(metaclass=SingletonMeta):
    """
    Storage Singleton used to communicate with the server
    """

    def __init__(self):
        server_config = Config.get_server_config()
        self.__enable_request = server_config.get("enabled", False)
        api_key = server_config.get("apiKey", "")
        self.__headers = {"X-API-Key": api_key.strip()}
        self.__base_url = server_config.get("url", "http://localhost:8080")

        self.__upload_queue: queue.Queue[MediaUploadObject] = queue.Queue()
        self._process_old_queue_files()
        threading.Thread(target=self._media_upload_worker, daemon=True).start()

    def create_paths(self, key: str):
        marshal_path = os.path.dirname(self._get_marshal_file(key))
        transcript_path = os.path.dirname(self._get_transcript_file(key))
        queue_path = self._get_queue_folder(key)
        if marshal_path:
            os.makedirs(marshal_path, exist_ok=True)
        if transcript_path:
            os.makedirs(transcript_path, exist_ok=True)
        if queue_path:
            os.makedirs(queue_path, exist_ok=True)

    def activate(self, info: StreamInfoObject):
        """Sets the active id to the stream provided and sends new info to server.
        If this is a new stream:
            then we reset the current state to the stream provided. And set status to live
        Else:
            We just update the title, start time, and set status to live.

        Args:
            info (StreamInfoObject): stream info we want to activate.
        """
        start_time = time.time()
        active_id = self._get_active_id(info.key)
        if info.stream_id != active_id:
            logger.info(f"[{info.key}][activate] New stream id. Resetting data")
            self._dict_to_file(
                info.key,
                {
                    "activeId": info.stream_id,
                    "activeTitle": info.stream_title,
                    "startTime": info.start_time,
                    "mediaType": info.media_type,
                    "isLive": True,
                    "transcript": [],
                },
            )
            if not self.__enable_request:
                # request disabled, so we reset the local file
                with open(self._get_transcript_file(info.key), "w") as f:
                    f.write(f"Activating stream {info.stream_title} [{info.stream_id}] started at [{info.start_time}]\n")

            # Clear upload queue and delete queue files
            while not self.__upload_queue.empty():
                try:
                    self.__upload_queue.get_nowait()
                    self.__upload_queue.task_done()
                except queue.Empty:
                    break
            self._clear_queue_folder(info.key)

        else:
            logger.info(f"[{info.key}][activate] Same stream id. Updating isLive")
            data = self._file_to_dict(info.key)
            data["isLive"] = True
            data["activeTitle"] = info.stream_title
            data["startTime"] = info.start_time
            self._dict_to_file(info.key, data)

        if self.__enable_request:
            url = f"{self.__base_url}/{info.key}"
            logger.debug(
                f"[{info.key}][activate] sending request id={info.stream_id} title={info.stream_title} startTime={info.start_time} mediaType={info.media_type}"
            )
            storage_time = time.time() - start_time
            try:
                response = httpx.post(
                    f"{url}/activate?id={quote(info.stream_id)}&title={quote(info.stream_title)}&startTime={quote(info.start_time)}&mediaType={quote(info.media_type)}",
                    headers=self.__headers,
                    timeout=None,
                )
                storage_time = time.time() - start_time
                if response.status_code != 200:
                    logger.warning(
                        f"[{info.key}][activate][{(storage_time):.3f}] Relay did not accept activation request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.info(f"[{info.key}][activate][{(storage_time):.3f}] Stream {info.stream_id} successfully activated")
            except httpx.RequestError as e:
                logger.error(f"[{info.key}][activate][{(storage_time):.3f}] Unable to send activation request to relay: {e}")

    def deactivate(self, key: str, stream_id: str):
        """Sets stream status to not live if the stream id is the same as the current active id. Then sends new info to server.

        Args:
            key (str): server key
            stream_id (str): the id of the stream
        """
        start_time = time.time()
        data = self._file_to_dict(key)
        data["isLive"] = False
        self._dict_to_file(key, data)

        if self.__enable_request and stream_id != "":
            url = f"{self.__base_url}/{key}"
            storage_time = time.time() - start_time
            try:
                response = httpx.post(f"{url}/deactivate?id={quote(stream_id)}", headers=self.__headers, timeout=None)
                storage_time = time.time() - start_time
                if response.status_code != 200:
                    logger.warning(
                        f"[{key}][deactivate][{(storage_time):.3f}] Relay did not accept deactivation request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.info(f"[{key}][deactivate][{(storage_time):.3f}] Stream {stream_id} successfully deactivated")
            except httpx.RequestError as e:
                logger.error(f"[{key}][deactivate][{(storage_time):.3f}] Unable to send deactivation request to relay: {e}")
        else:
            # local only, so we should log
            logger.info(f"[{key}][deactivate] Stream {stream_id} successfully deactivated")

    def add_new_line(self, key: str, line: dict, raw_bytes: bytes | None):
        """Sends new line transcript to the server. Automatically sets the line id to the next number.
        If the server is out of sync, and responds with 409, then we call sync_server to reset the servers state.

        Args:
            key (str): server key
            line (dict): {'id': -1, 'timestamp': 123, 'segments': [{'timestamp' 123, 'text': 'abc'}]}
            raw_bytes (bytes | None): the raw media binary. The type of media is determined by what the stream was activated with. If None, then no media is uploaded.
        """
        storage_start_time = time.time()
        data = self._file_to_dict(key)
        transcript: list = data["transcript"]
        last_id = -1
        if len(transcript) > 0:
            last_id = transcript[-1]["id"]
        line["id"] = last_id + 1
        line["mediaAvailable"] = False
        transcript.append(line)
        data["transcript"] = transcript
        self._dict_to_file(key, data)

        if self.__enable_request:
            url = f"{self.__base_url}/{key}"
            storage_time = time.time() - storage_start_time
            try:
                response = httpx.post(f"{url}/line", headers=self.__headers, json=line, timeout=None)
                storage_time = time.time() - storage_start_time
                if response.status_code == 409:
                    self.sync_server(key, data)
                    # Add media to queue after sync so that this line's media doesn't get missed.
                    self._enqueue_media(key, line["id"], raw_bytes)
                elif response.status_code != 200:
                    logger.warning(
                        f"[{key}][add_new_line][{(storage_time):.3f}] Relay did not accept line request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.debug(f"[{key}][add_new_line][{(storage_time):.3f}] successfully sent {line}")
                    # We need to enqueue after the line is sent so that the server bc the server needs the line to exist before it can add the media
                    self._enqueue_media(key, line["id"], raw_bytes)
            except httpx.RequestError as e:
                logger.error(f"Unable to send line request to relay: {e}")
        else:
            # request disabled, so we append new line to local file
            line_text = []
            line_time = line["timestamp"]
            start_time = int(self._file_to_dict(key).get("startTime", "0"))
            if "segments" in line:
                for segment in line["segments"]:
                    if "text" in segment:
                        line_text.append(segment["text"])
            total_seconds = line_time - start_time
            hours = total_seconds // 3600
            remaining_seconds = total_seconds % 3600
            minutes = remaining_seconds // 60
            seconds = remaining_seconds % 60
            timestamp = (
                f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                if start_time > 0
                else f"{datetime.fromtimestamp(line_time - start_time).strftime('%H:%M:%S')}"
            )
            with open(self._get_transcript_file(key), "a") as f:
                f.write(f"[{timestamp}] {' '.join(line_text)}\n")
            storage_time = time.time() - storage_start_time
            logger.debug(f"[{key}][add_new_line][{(storage_time):.3f}] successfully wrote {line}")

    def sync_server(self, key: str, data):
        """Called when the server is out of sync and we need to reset the servers state.
        This will send the entire current state to the server. Forcing it to reset to it.

        Args:
            key (str): server key
            data (_type_): current state for the given key
        """
        start_time = time.time()
        if self.__enable_request:
            url = f"{self.__base_url}/{key}"
            storage_time = time.time() - start_time
            try:
                response = httpx.post(f"{url}/sync", headers=self.__headers, json=data, timeout=None)
                storage_time = time.time() - start_time
                if response.status_code != 200:
                    logger.warning(
                        f"[{key}][sync_server][{(storage_time):.3f}] Relay did not accept sync request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.info(f"[{key}][sync_server][{(storage_time):.3f}] Uploaded entire state to server")
            except httpx.RequestError as e:
                logger.error(f"[{key}][sync_server][{(storage_time):.3f}] Unable to send sync request to relay: {e}")

    def _get_marshal_file(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        marshal_path = os.path.join(project_root_dir, "tmp", key, "data.marshal")
        return marshal_path

    def _get_transcript_file(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        transcript_path = os.path.join(project_root_dir, "tmp", key, "transcript.text")
        return transcript_path

    def _get_queue_folder(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        queue_path = os.path.join(project_root_dir, "tmp", key, "queue")
        return queue_path

    def _get_active_id(self, key: str) -> str:
        initial_state = self._file_to_dict(key)
        return initial_state["activeId"]

    def _file_to_dict(self, key: str) -> dict:
        data = {"activeId": ""}
        try:
            with open(self._get_marshal_file(key), "rb") as file:
                data = marshal.load(file)
        except Exception:
            pass

        return data

    def _dict_to_file(self, key: str, data: dict):
        # Serialize the dictionary to a file
        try:
            with open(self._get_marshal_file(key), "wb") as file:
                marshal.dump(data, file)
        except Exception:
            pass

    def _enqueue_media(self, key: str, line_id: int, raw_bytes: bytes | None):
        """Saves media to disk and enqueues it for upload"""

        if not raw_bytes or len(raw_bytes) == 0:
            return

        queue_folder = self._get_queue_folder(key)
        media_path = os.path.join(queue_folder, f"media_{line_id}.bin")
        try:
            with open(media_path, "wb") as f:
                f.write(raw_bytes)
            new_media = MediaUploadObject(key, line_id, media_path)
            self.__upload_queue.put(new_media)
        except Exception as e:
            logger.error(f"[{key}][enqueue_media] Error saving media to disk: {e}")

    def _clear_queue_folder(self, key):
        """Clears the queue folder for the given key. First deletes the folder then recreates it"""
        queue_folder = self._get_queue_folder(key)
        logger.debug(f"[{key}][clear_queue_folder] clearing queue folder {queue_folder}")

        if os.path.exists(queue_folder):
            try:
                shutil.rmtree(queue_folder)
            except OSError as e:
                logger.error(f"[{key}][clear_queue_folder] Error deleting queue folder {queue_folder}: {e}")
            except Exception as e:
                logger.error(f"[{key}][clear_queue_folder] unknown error deleting queue foler {queue_folder}: {e}")

        # Recreate the empty folder
        try:
            os.makedirs(queue_folder)
        except OSError as e:
            logger.error(f"[{key}][clear_queue_folder] Error recreating queue folder {queue_folder}: {e}")
        except Exception as e:
            logger.error(f"[{key}][clear_queue_folder] unknown error recreating queue foler {queue_folder}: {e}")

        logger.debug(f"[{key}][clear_queue_folder] successfully cleared queue folder")

    def _media_upload_worker(self):
        while True:
            item = self.__upload_queue.get()
            start_time = time.time()
            if os.path.exists(item.path):
                if self.__enable_request:
                    url = f"{self.__base_url}/{item.key}"
                    try:
                        with open(item.path, "rb") as f:
                            files = {"file": f}
                            response = httpx.post(f"{url}/media/{item.id}", headers=self.__headers, files=files, timeout=None)
                        storage_time = time.time() - start_time
                        if response.status_code != 200:
                            logger.warning(
                                f"[{item.key}][upload_media][{item.id}][{(storage_time):.3f}] Relay did not accept media upload. Response: {response.status_code} {response.text}"
                            )
                        else:
                            logger.debug(f"[{item.key}][upload_media][{item.id}][{(storage_time):.3f}] successfully uploaded media")
                    except Exception as e:
                        logger.error(f"[{item.key}][upload_media][{item.id}] Error uploading media: {e}")

                # Delete file after attempt
                try:
                    os.remove(item.path)
                except Exception as e:
                    logger.error(f"[{item.key}][upload_media][{item.id}] Error deleting file {item.path}: {e}")

            self.__upload_queue.task_done()

    def _process_old_queue_files(self):
        """Processes old files in the queue folder and adds them to the upload queue in BFS order."""
        streamers = Config.get_all_streamers_config()
        if not streamers:
            return

        all_keys_files: dict[str, list[tuple[int, str]]] = {}
        for streamer in streamers:
            key: str | None = streamer.get("key")
            if not key:
                continue
            queue_folder: str = self._get_queue_folder(key)
            if not os.path.exists(queue_folder) or not os.path.isdir(queue_folder):
                continue

            files: list[tuple[int, str]] = []
            for filename in os.listdir(queue_folder):
                if filename.startswith("media_") and filename.endswith(".bin"):
                    try:
                        line_id = int(filename[6:-4])
                        files.append((line_id, os.path.join(queue_folder, filename)))
                    except ValueError:
                        continue

            if files:
                files.sort()  # Sort by line_id to ensure order within each key
                all_keys_files[key] = files

        if not all_keys_files:
            return

        # Interleave files from different keys (BFS-like ordering)
        sorted_keys = sorted(all_keys_files.keys())
        max_files = max(len(f) for f in all_keys_files.values())

        for i in range(max_files):
            for key in sorted_keys:
                if i < len(all_keys_files[key]):
                    line_id, path = all_keys_files[key][i]
                    logger.info(f"[{key}][storage] Enqueuing old media file: {path}")
                    self.__upload_queue.put(MediaUploadObject(key, line_id, path))

    def wait_for_uploads(self, timeout: float = 30):
        """Waits for the upload queue to be empty.

        Args:
            timeout (float): Max time to wait in seconds. Defaults to 30.
        """
        logger.info(f"[storage] Waiting up to {timeout}s for uploads to finish...")
        end_time = time.time() + timeout
        # Sleep to give _enqueue_media enough time to write any files to disk and enqueue them.
        time.sleep(3)
        while not self.__upload_queue.empty():
            if time.time() > end_time:
                logger.warning("[storage] Timeout waiting for uploads to finish.")
                return
            time.sleep(0.5)
        logger.info("[storage] All uploads finished.")
