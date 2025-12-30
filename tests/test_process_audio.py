from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, ProcessObject
from src.live_transcript_worker.process_audio import ProcessAudio


@pytest.fixture
def mock_storage_process(mocker):
    return mocker.patch("src.live_transcript_worker.process_audio.Storage")


@pytest.fixture
def process_audio_instance(mock_storage_process, mock_config, mocker):
    mocker.patch("src.live_transcript_worker.process_audio.WhisperModel")
    ready_event = MagicMock()
    pa = ProcessAudio(ready_event)
    return pa


def test_load_model(process_audio_instance):
    assert process_audio_instance.whisper_model is not None


def test_unload_model(process_audio_instance):
    process_audio_instance.unload_model()
    assert process_audio_instance.whisper_model is None


def test_decensor(process_audio_instance):
    assert process_audio_instance.decensor("f**k") == "fuck"
    assert process_audio_instance.decensor("F**k") == "Fuck"
    assert process_audio_instance.decensor("normal text") == "normal text"
    assert process_audio_instance.decensor("a** and b**ch") == "ass and bitch"


def test_transcribe_success(process_audio_instance, mocker):
    # Mock model
    mock_model = process_audio_instance.whisper_model

    # helper for segment
    Segment = MagicMock
    s1 = Segment(start=0.0, text="hello")
    s2 = Segment(start=1.0, text="world")

    mock_model.transcribe.return_value = ([s1, s2], MagicMock(duration=5.0))

    segments, duration = process_audio_instance.transcribe(b"data")

    assert len(segments) == 2
    assert duration == 5.0
    assert segments[0] == (0.0, "hello")


def test_transcribe_short_duration(process_audio_instance, mocker):
    mock_model = process_audio_instance.whisper_model
    mock_model.transcribe.return_value = ([], MagicMock(duration=0.1))

    result = process_audio_instance.transcribe(b"data")
    assert result is None


def test_transcribe_error(process_audio_instance, mocker):
    mock_model = process_audio_instance.whisper_model
    mock_model.transcribe.side_effect = Exception("error")

    segments, duration = process_audio_instance.transcribe(b"data")
    assert segments == []
    assert duration == -1.0


def test_process_audio_full_flow(process_audio_instance, mock_storage_process, mocker):
    # Mock transcribe
    mocker.patch.object(process_audio_instance, "transcribe", return_value=([(0.0, "hello"), (1.0, "world")], 5.0))

    item = ProcessObject(raw=b"data", audio_start_time=100.0, key="key", media_type=Media.AUDIO)

    process_audio_instance.process_audio(item)

    # assert storage add_new_line called
    mock_storage_instance = mock_storage_process.return_value
    mock_storage_instance.add_new_line.assert_called_once()

    call_args = mock_storage_instance.add_new_line.call_args
    assert call_args[0][0] == "key"  # key
    line = call_args[0][1]
    assert line["timestamp"] == 100
    assert len(line["segments"]) == 2
    assert line["segments"][0]["text"] == "hello"


def test_process_audio_no_transcription(process_audio_instance, mock_storage_process, mocker):
    mocker.patch.object(process_audio_instance, "transcribe", return_value=None)

    item = ProcessObject(raw=b"data", audio_start_time=100.0, key="key", media_type=Media.AUDIO)

    process_audio_instance.process_audio(item)

    mock_storage_instance = mock_storage_process.return_value
    mock_storage_instance.add_new_line.assert_not_called()
