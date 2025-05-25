import base64
from io import BytesIO
import logging
from math import floor
from threading import Event
import time

from faster_whisper import WhisperModel

from src.live_transcript_worker.config import Config
from src.live_transcript_worker.types import Media, ProcessObject
from src.live_transcript_worker.storage import Storage

logger = logging.getLogger(__name__)

class ProcessAudio(object):
    """
    Processes audio chunks, transcribes the audio chunks, and upload the results to the server.
    """

    def __init__(self, ready_event: Event):
        self.storage = Storage()
        config = Config.get_transcription_config()
        model = config.get("model", "base")
        device = config.get("device", "cpu")
        compute_type = config.get("compute_type", "int8")
        download_root = "./models"
        logger.info(f"Loading model {model} with device {device} using type {compute_type}...")
        self.whisper_model = WhisperModel(
            model, device, compute_type=compute_type, download_root=download_root
        )
        logger.info("Done.")
        ready_event.set()

    def process_audio(self, item: ProcessObject):
        start_time = time.time()
        with BytesIO(item.raw) as data:
            transcription_start = time.time()
            items = self.transcribe(data)
            transcription_time = time.time() - transcription_start

        if items is None:
            # Audio is too short to be considered as a transcription line.
            return

        segments, duration = items
        new_segments = []
        for segment in segments:
            segment_timestamp, text = segment
            new_segment = {
                "timestamp": floor(item.audio_start_time + segment_timestamp),
                "text": text,
            }
            new_segments.append(new_segment)

        new_line = {
            "id": -1,  # we let storage take care of setting the line id
            "timestamp": floor(item.audio_start_time),
            "segments": new_segments,
        }

        raw_b64 = ""
        if item.media_type != Media.NONE:
            raw_b64 = base64.b64encode(item.raw).decode("utf-8")
        total_processing_time = time.time() - start_time
        if duration < 0:
            duration_time_str = "ERROR"
        else:
            duration_time_str = f"duration: {duration:.3f}"

        logger.info(f"[{item.key}][process_audio] time: {(total_processing_time):.3f}, t_time:{transcription_time:.3f}, {duration_time_str}, size: {(len(raw_b64.encode('utf8')) / 1024.0):.3f} KiB")
        self.storage.update(item.key, new_line, raw_b64)

    def transcribe(self, data: BytesIO) -> tuple[list[tuple[float, str]], float] | None:
        """Transcribes the audio into segments.

        Args:
            data (BytesIO): a BytesIO object of the raw binary audio data.

        Returns: None if the audio is too short, or a list of segment tuples (start_time, text)
        """
        try:
            segments, info = self.whisper_model.transcribe(
                data,
                language="en",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=100),
            )

            if segments is None or info is None:
                return [], -1.0
            if info.duration < 0.5:
                # duration is small usually when we are using FixedBitrateWorker and an ad starts playing. So we skip it.
                return None

            new_segments: list[tuple[float, str]] = []
            for segment in segments:
                text = self.decensor(segment.text.strip())
                new_segments.append((segment.start, text))

            return new_segments, info.duration

        except Exception as e:
            # This usually gets called when faster_whisper failed to decode the audio.
            # Usually of the form av.error.UndefinedError: [Errno 67308554] Error number -67308554 occurred: 'avcodec_send_packet()'
            # Best to ignore and move on
            logger.debug(f"Error during transcription of data: {e}")
            return [], -1.0

    def decensor(self, text: str) -> str:
        # Case sensitive. We replace for both lowercase and uppercase versions. So it's best to only have lowercase here.
        # "old_word1": "new_word1"
        word_map = {
            "f**k": "fuck",
            "f***ing": "fucking",
            "f*****g": "fucking",
            "f******": "fucking",
            "fuck***t": "fucking bullshit",
            "fuck***": "fucking",
            "f**ing": "fucking",
            "f*****": "fucker",
            "f***": "fuck",
            "f**": "fuck",
            "sh**": "shit",
            "s**t": "shit",
            "s***": "shit",
            "a**": "ass",
            "b**ch": "bitch",
            "b***h": "bitch",
            "c***": "cunt",
            "p***y": "pussy",
            "d**n": "damn",
            "****": "fuck",
        }

        for old_word, new_word in word_map.items():
            text = text.replace(old_word.lower(), new_word.lower())
            text = text.replace(old_word.capitalize(), new_word.capitalize())

        return text
