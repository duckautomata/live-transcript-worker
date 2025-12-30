import os
from threading import Event

import pytest

from src.live_transcript_worker.custom_types import Media, ProcessObject
from src.live_transcript_worker.process_audio import ProcessAudio


@pytest.mark.integration
@pytest.mark.parametrize(
    "audio_filename, min_chars, max_chars, expected_words",
    [
        ("test-1.mp3", 40, 250, ["What", "nothing", "believe"]),
        ("test-2.mp3", 5, 100, ["What"]),
        ("test-3.mp3", 0, 100, ["up"]),
        ("test-4.mp3", 10, 100, ["doing"]),
        ("test-5.mp3", 10, 100, ["nothing"]),
        ("1-to-10.mp3", 10, 100, ["1, 2, 3, 4, 5, 6, 7, 8, 9, 10"]),
        ("10-to-1.mp3", 10, 100, ["10, 9, 8, 7, 6, 5, 4, 3, 2, 1"]),
    ],
)
def test_real_whisper_transcription(mocker, audio_filename, min_chars, max_chars, expected_words):
    """
    Integration test that runs the actual Whisper model on a sample audio file.
    Verifies that the model loads and produces a non-empty transcription.
    """
    import logging

    logging.basicConfig(level=logging.DEBUG)

    # 1. Setup paths
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    audio_path = os.path.join(project_root, "tests", "audio", audio_filename)

    if not os.path.exists(audio_path):
        pytest.skip(f"Audio file not found at {audio_path}")

    # 2. Mock Storage and Config (we don't want to hit the network or use GPU)
    mock_storage_cls = mocker.patch("src.live_transcript_worker.process_audio.Storage")
    mock_config_cls = mocker.patch("src.live_transcript_worker.process_audio.Config")
    mock_config_cls.get_transcription_config.return_value = {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
    }

    # 3. Instantiate ProcessAudio
    # This will trigger load_model() which downloads the model if needed.
    ready_event = Event()
    # ProcessAudio sets the event in __init__ after loading
    pa = ProcessAudio(ready_event)

    assert pa.whisper_model is not None

    # 4. Read audio file
    with open(audio_path, "rb") as f:
        raw_audio = f.read()

    # 5. Create ProcessObject
    # We set audio_start_time to 100.0 to verify the timestamp logic
    item = ProcessObject(raw=raw_audio, audio_start_time=100.0, key="test_integration_key", media_type=Media.AUDIO)

    # 6. Run process_audio
    # This calls transcribe, then storage.update
    pa.process_audio(item)

    # 7. Verify Results
    # Check that storage.add_new_line was called with valid data
    mock_instance = mock_storage_cls.return_value
    assert mock_instance.add_new_line.called

    args = mock_instance.add_new_line.call_args[0]
    key = args[0]
    data = args[1]

    # Verify key
    assert key == "test_integration_key"

    # Verify timestamp (should be floor(100.0) = 100)
    assert data["timestamp"] == 100, f"Expected timestamp 100, got {data.get('timestamp')}"

    # Reconstruct text from segments
    full_text = " ".join([seg["text"] for seg in data["segments"]])
    char_count = len(full_text)

    # Print for debug
    print(f"\n[{audio_filename}] Text ({char_count} chars): {full_text}")

    # Verify lengths
    assert char_count >= min_chars, f"Text length {char_count} < min {min_chars}"
    assert char_count <= max_chars, f"Text length {char_count} > max {max_chars}"

    # Verify expected words
    for word in expected_words:
        assert word.lower() in full_text.lower(), f"Expected word '{word}' not found in text"
