import os
from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, MediaUploadObject
from src.live_transcript_worker.storage import Storage


@pytest.fixture
def storage(mock_config, mocker):
    # Ensure usage of mock_config which mocks Config used in Storage.__init__

    # We need to recreate the singleton or reset it
    Storage._instances = {}

    # Patch Config in storage module just to be sure
    mocker.patch("src.live_transcript_worker.storage.Config", mock_config)

    return Storage()


def test_singleton(storage):
    s2 = Storage()
    assert storage is s2


def test_create_paths(storage, tmp_path, mocker):
    mocker.patch.object(storage, "_get_marshal_file", return_value=str(tmp_path / "key" / "data.marshal"))
    mocker.patch.object(storage, "_get_transcript_file", return_value=str(tmp_path / "key" / "transcript.text"))
    mocker.patch.object(storage, "_get_queue_folder", return_value=str(tmp_path / "key" / "queue"))

    storage.create_paths("key")

    assert os.path.exists(str(tmp_path / "key"))
    assert os.path.exists(str(tmp_path / "key" / "queue"))


def test_activate_new_stream(storage, mocker):
    mock_dict_to_file = mocker.patch.object(storage, "_dict_to_file")
    mocker.patch.object(storage, "_get_active_id", return_value="old_id")
    mock_clear_queue = mocker.patch.object(storage, "_clear_queue_folder")

    # mock http request
    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=200)

    info = MagicMock()
    info.key = "test_key"
    info.stream_id = "new_id"
    info.stream_title = "Title"
    info.start_time = "100"
    info.media_type = Media.AUDIO

    # mock queue to verify clearing
    storage._Storage__upload_queue = MagicMock()
    storage._Storage__upload_queue.empty.side_effect = [False, True]  # One item then empty
    storage._Storage__upload_queue.get_nowait.return_value = "item"

    storage.activate(info)

    mock_dict_to_file.assert_called_with(
        "test_key",
        {
            "activeId": "new_id",
            "activeTitle": "Title",
            "startTime": "100",
            "mediaType": Media.AUDIO,
            "isLive": True,
            "transcript": [],
        },
    )
    mock_clear_queue.assert_called_with("test_key")
    storage._Storage__upload_queue.get_nowait.assert_called()
    storage.client.post.assert_called()


def test_activate_same_stream(storage, mocker):
    mock_dict_to_file = mocker.patch.object(storage, "_dict_to_file")
    mocker.patch.object(storage, "_get_active_id", return_value="same_id")
    mocker.patch.object(storage, "_file_to_dict", return_value={"isLive": False})

    # mock http request
    # mock http request
    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=200)

    info = MagicMock()
    info.key = "test_key"
    info.stream_id = "same_id"
    info.stream_title = "Title"
    info.start_time = "100"
    info.media_type = "audio"

    storage.activate(info)

    mock_dict_to_file.assert_called()
    args, _ = mock_dict_to_file.call_args
    assert args[1]["isLive"] is True


def test_deactivate(storage, mocker):
    mock_dict_to_file = mocker.patch.object(storage, "_dict_to_file")
    mocker.patch.object(storage, "_file_to_dict", return_value={"isLive": True})

    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=200)

    storage.deactivate("key", "id")

    mock_dict_to_file.assert_called()
    args, _ = mock_dict_to_file.call_args
    assert args[1]["isLive"] is False


def test_add_new_line(storage, mocker, tmp_path):
    mocker.patch.object(storage, "_file_to_dict", return_value={"activeId": "a12", "transcript": [{"id": 0}], "startTime": 0})
    mock_dict_to_file = mocker.patch.object(storage, "_dict_to_file")
    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=200)

    # Mock queue folder
    queue_folder = tmp_path / "queue"
    queue_folder.mkdir()
    mocker.patch.object(storage, "_get_queue_folder", return_value=str(queue_folder))

    # Mock upload queue
    # Mock upload queue
    storage._Storage__upload_queue = MagicMock()

    raw_bytes = b"base64data"
    storage.add_new_line("key", {"timestamp": 100}, raw_bytes)

    # Verify transcript updated
    mock_dict_to_file.assert_called()
    updated_data = mock_dict_to_file.call_args[0][1]
    assert len(updated_data["transcript"]) == 2
    assert updated_data["transcript"][-1]["id"] == 1
    assert updated_data["transcript"][-1]["mediaAvailable"] is False

    # Verify file saved
    media_file = queue_folder / "media_stream_id=a12 line_id=1.bin"
    assert media_file.exists()
    assert media_file.read_bytes() == raw_bytes

    # Verify enqueued
    storage._Storage__upload_queue.put.assert_called_with(MediaUploadObject("key", "a12", 1, str(media_file)))


def test_add_new_line_sync_error(storage, mocker, tmp_path):
    mocker.patch.object(storage, "_file_to_dict", return_value={"activeId": "345", "transcript": [], "startTime": 0})
    mocker.patch.object(storage, "_dict_to_file")
    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=409)
    mock_sync = mocker.patch.object(storage, "sync_server")

    # Mock queue stuff to avoid errors
    queue_folder = tmp_path / "queue"
    queue_folder.mkdir()
    mocker.patch.object(storage, "_get_queue_folder", return_value=str(queue_folder))

    storage.add_new_line("key", {"timestamp": 100}, b"")

    mock_sync.assert_called()


def test_media_upload_worker(storage, mocker, tmp_path):
    # Create a real file
    file_path = tmp_path / "media_stream_id=123 line_id=2.bin"
    file_path.write_bytes(b"content")

    # Mock queue.get to return item then raise exception to break loop
    storage._Storage__upload_queue = MagicMock()
    storage._Storage__upload_queue.get.side_effect = [MediaUploadObject("key", "123", 2, str(file_path)), Exception("Stop Loop")]

    storage.client = MagicMock()
    storage.client.post.return_value = MagicMock(status_code=200)
    mock_post = storage.client.post

    try:
        storage._media_upload_worker()
    except Exception as e:
        if str(e) != "Stop Loop":
            raise e

    # CHECK: httpx.post called with file
    assert mock_post.called
    args, kwargs = mock_post.call_args
    # url check
    assert "key/media/1" in args[0]
    # files check
    assert "file" in kwargs["files"]

    # Verify file deleted
    assert not file_path.exists()


def test_process_old_queue_files_bfs_uneven(mocker, tmp_path, mock_config):
    # Reset singleton and ensure it uses mock_config
    Storage._instances = {}
    mocker.patch("src.live_transcript_worker.storage.Config", mock_config)
    mocker.patch("threading.Thread")  # Don't start worker thread

    # Setup streamers config
    streamers = [{"key": "test1"}, {"key": "test2"}]
    mock_config.get_all_streamers_config.return_value = streamers

    storage = Storage()

    # Mock _get_queue_folder to use tmp_path
    mocker.patch.object(storage, "_get_queue_folder", side_effect=lambda key: str(tmp_path / key / "queue"))

    # Create directories
    (tmp_path / "test1" / "queue").mkdir(parents=True)
    (tmp_path / "test2" / "queue").mkdir(parents=True)

    # Create files for test1: 3 files (id 10, 11, 12) media_stream_id=abc line_id=10.bin
    (tmp_path / "test1" / "queue" / "media_stream_id=abc line_id=10.bin").write_bytes(b"t1-10")
    (tmp_path / "test1" / "queue" / "media_stream_id=abc line_id=11.bin").write_bytes(b"t1-11")
    (tmp_path / "test1" / "queue" / "media_stream_id=abc line_id=12.bin").write_bytes(b"t1-12")

    # Create files for test2: 1 file (id 5)
    (tmp_path / "test2" / "queue" / "media_stream_id=def line_id=5.bin").write_bytes(b"t2-5")

    # Clear current queue (it was populated in __init__)
    while not storage._Storage__upload_queue.empty():
        storage._Storage__upload_queue.get()
        storage._Storage__upload_queue.task_done()

    storage._process_old_queue_files()

    q = storage._Storage__upload_queue
    assert q.qsize() == 4

    # Expected order (BFS): test1_file1, test2_file1, test1_file2, test1_file3
    # test1 files sorted by id: 10, 11, 12
    # test2 files sorted by id: 5

    # Round 1:
    # test1 -> media_10
    item = q.get()
    assert item.key == "test1" and item.id == 10
    # test2 -> media_5
    item = q.get()
    assert item.key == "test2" and item.id == 5

    # Round 2:
    # test1 -> media_11
    item = q.get()
    assert item.key == "test1" and item.id == 11
    # test2 -> empty

    # Round 3:
    # test1 -> media_12
    item = q.get()
    assert item.key == "test1" and item.id == 12


def test_process_old_queue_files_empty(mocker, mock_config, tmp_path):
    # Reset singleton
    Storage._instances = {}
    mocker.patch("src.live_transcript_worker.storage.Config", mock_config)
    mocker.patch("threading.Thread")

    # Mock _get_queue_folder to use tmp_path so we don't pick up real files
    mocker.patch("src.live_transcript_worker.storage.Storage._get_queue_folder", side_effect=lambda key: str(tmp_path / key / "queue"))

    # Case 1: No streamers
    mock_config.get_all_streamers_config.return_value = []
    storage = Storage()
    storage._process_old_queue_files()
    assert storage._Storage__upload_queue.empty()

    # Case 2: No files
    Storage._instances = {}
    mock_config.get_all_streamers_config.return_value = [{"key": "test"}]
    storage = Storage()
    storage._process_old_queue_files()
    assert storage._Storage__upload_queue.empty()
