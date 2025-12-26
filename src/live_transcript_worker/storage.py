import base64
import logging
import marshal
import os
import shutil
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from src.live_transcript_worker.config import Config
from src.live_transcript_worker.custom_types import Media, StreamInfoObject

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
        self.__enable_dump_media = server_config.get("enable_dump_media", False)

    def create_paths(self, key: str):
        marshal_path = os.path.dirname(self.__get_marshal_file(key))
        transcript_path = os.path.dirname(self.__get_transcript_file(key))
        dump_path = self.__get_dump_folder(key)
        if marshal_path:
            os.makedirs(marshal_path, exist_ok=True)
        if transcript_path:
            os.makedirs(transcript_path, exist_ok=True)
        if dump_path:
            os.makedirs(dump_path, exist_ok=True)

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
        active_id = self.__get_active_id(info.key)
        if info.stream_id != active_id:
            logger.info(f"[{info.key}][activate] New stream id. Resetting data")
            self.__dict_to_file(
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
                with open(self.__get_transcript_file(info.key), "w") as f:
                    f.write(f"Activating stream {info.stream_title} [{info.stream_id}] started at [{info.start_time}]\n")
            self.__clear_dump_folder(info.key)

        else:
            logger.info(f"[{info.key}][activate] Same stream id. Updating isLive")
            data = self.__file_to_dict(info.key)
            data["isLive"] = True
            data["activeTitle"] = info.stream_title
            data["startTime"] = info.start_time
            self.__dict_to_file(info.key, data)

        if self.__enable_request:
            url = f"{self.__base_url}/{info.key}"
            logger.debug(
                f"[{info.key}][activate] sending request id={info.stream_id} title={info.stream_title} startTime={info.start_time} mediaType={info.media_type}"
            )
            storage_time = time.time() - start_time
            try:
                response = httpx.get(
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
        data = self.__file_to_dict(key)
        data["isLive"] = False
        self.__dict_to_file(key, data)

        if self.__enable_request and stream_id != "":
            url = f"{self.__base_url}/{key}"
            storage_time = time.time() - start_time
            try:
                response = httpx.get(f"{url}/deactivate?id={quote(stream_id)}", headers=self.__headers, timeout=None)
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

    def update(self, key: str, line: dict, raw_b64_data: str):
        """Sends new line transcript to the server. Automatically sets the line id to the next number.
        If the server is out of sync, and responds with 409, then we call __upload to reset the servers state.

        Args:
            key (str): server key
            line (dict): {'id': -1, 'segments': [{'timestamp' 123, 'text': 'abc'}]}
            raw_b64_data (str): base64 encoded of the media binary. The type of media is determined by what the stream was activated with.
        """
        storage_start_time = time.time()
        data = self.__file_to_dict(key)
        transcript: list = data["transcript"]
        last_id = -1
        if len(transcript) > 0:
            last_id = transcript[-1]["id"]
        line["id"] = last_id + 1
        transcript.append(line)
        data["transcript"] = transcript
        self.__dict_to_file(key, data)

        updateData = {"line": line, "rawB64Data": raw_b64_data}

        if self.__enable_request:
            url = f"{self.__base_url}/{key}"
            storage_time = time.time() - storage_start_time
            try:
                response = httpx.post(f"{url}/update", headers=self.__headers, json=updateData, timeout=None)
                storage_time = time.time() - storage_start_time
                if response.status_code == 409:
                    self.__upload(key, data)
                elif response.status_code != 200:
                    logger.warning(
                        f"[{key}][update][{(storage_time):.3f}] Relay did not accept update request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.debug(f"[{key}][update][{(storage_time):.3f}] successfully sent {line}")
            except httpx.RequestError as e:
                logger.error(f"Unable to send update request to relay: {e}")
        else:
            # request disabled, so we append new line to local file
            line_text = []
            line_time = line["timestamp"]
            start_time = int(self.__file_to_dict(key).get("startTime", "0"))
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
            with open(self.__get_transcript_file(key), "a") as f:
                f.write(f"[{timestamp}] {' '.join(line_text)}\n")
            storage_time = time.time() - storage_start_time
            logger.debug(f"[{key}][update][{(storage_time):.3f}] successfully wrote {line}")

        if self.__enable_dump_media:
            self.__dump_media(key, line["id"], raw_b64_data)

    def __upload(self, key: str, data):
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
                response = httpx.post(f"{url}/upload", headers=self.__headers, json=data, timeout=None)
                storage_time = time.time() - start_time
                if response.status_code != 200:
                    logger.warning(
                        f"[{key}][_upload][{(storage_time):.3f}] Relay did not accept upload request. Response: {response.status_code} {response.text}"
                    )
                else:
                    logger.info(f"[{key}][_upload][{(storage_time):.3f}] Uploaded entire state to server")
            except httpx.RequestError as e:
                logger.error(f"[{key}][_upload][{(storage_time):.3f}] Unable to send upload request to relay: {e}")

    def __get_marshal_file(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        marshal_path = os.path.join(project_root_dir, "tmp", key, "data.marshal")
        return marshal_path

    def __get_transcript_file(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        transcript_path = os.path.join(project_root_dir, "tmp", key, "transcript.text")
        return transcript_path

    def __get_dump_folder(self, key: str):
        project_root_dir = os.path.dirname(os.path.abspath(__name__))
        dump_path = os.path.join(project_root_dir, "tmp", key, "dump")
        return dump_path

    def __get_active_id(self, key: str) -> str:
        initial_state = self.__file_to_dict(key)
        return initial_state["activeId"]

    def __file_to_dict(self, key: str) -> dict:
        data = {"activeId": ""}
        try:
            with open(self.__get_marshal_file(key), "rb") as file:
                data = marshal.load(file)
        except Exception:
            pass

        return data

    def __dict_to_file(self, key: str, data: dict):
        # Serialize the dictionary to a file
        try:
            with open(self.__get_marshal_file(key), "wb") as file:
                marshal.dump(data, file)
        except Exception:
            pass

    def __dump_media(self, key, line_id: int, raw_b64_data: str):
        """Decodes the base64 media and writes it to a dump folder. Used for debugging"""
        media = self.__file_to_dict(key).get("mediaType", Media.AUDIO)
        if media == Media.NONE or len(raw_b64_data) == 0:
            # no data to dump.
            return
        dump_folder = self.__get_dump_folder(key)
        dump_path = os.path.join(dump_folder, f"{media}_{line_id:04d}.raw")
        try:
            decoded_data = base64.b64decode(raw_b64_data)
            with open(dump_path, "wb") as file:
                file.write(decoded_data)

        except IOError as e:
            logger.error(f"[{key}][dump_media] Error writing to file {dump_path}: {e}")
        except Exception as e:
            logger.error(f"[{key}][dump_media] An unexpected error occurred: {e}")

    def __clear_dump_folder(self, key):
        """Clears the dump folder for the given key. First deletes the folder then recreates it"""
        dump_folder = self.__get_dump_folder(key)
        logger.debug(f"[{key}][clear_dump_folder] clearing dump folder {dump_folder}")

        if os.path.exists(dump_folder):
            try:
                shutil.rmtree(dump_folder)
            except OSError as e:
                logger.error(f"[{key}][clear_dump_folder] Error deleting dump folder {dump_folder}: {e}")
            except Exception as e:
                logger.error(f"[{key}][clear_dump_folder] unknown error deleting dump foler {dump_folder}: {e}")

        # Recreate the empty folder
        try:
            os.makedirs(dump_folder)
        except OSError as e:
            logger.error(f"[{key}][clear_dump_folder] Error recreating dump folder {dump_folder}: {e}")
        except Exception as e:
            logger.error(f"[{key}][clear_dump_folder] unknown error recreating dump foler {dump_folder}: {e}")

        logger.debug(f"[{key}][clear_dump_folder] successfully cleared dump folder")
