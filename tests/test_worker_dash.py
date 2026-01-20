import os
from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, StreamInfoObject
from src.live_transcript_worker.worker_dash import DASHWorker


@pytest.fixture
def dash_worker(mocker):
    mocker.patch("src.live_transcript_worker.worker_abstract.Config")
    # Mock AbstractWorker init items
    queue = MagicMock()
    stop_event = MagicMock()
    stop_event.is_set.side_effect = [False, True]  # Run loop once then stop

    worker = DASHWorker("key", queue, stop_event)
    worker.stale_time_threshold = 10  # Set stale threshold
    return worker


def test_initialization(dash_worker):
    assert dash_worker.key == "key"


def test_start_new_stream(dash_worker, mocker):
    mocker.patch.object(dash_worker, "_load_state", return_value=(0, 100.0))
    mocker.patch.object(dash_worker, "_cleanup")
    mocker.patch("os.makedirs")
    mocker.patch.object(dash_worker, "create_process", return_value=MagicMock())
    mocker.patch.object(dash_worker, "_monitor_loop")

    info = StreamInfoObject(url="http://test", key="key", start_time="100.0", stream_id="id")

    dash_worker.start(info)

    dash_worker._cleanup.assert_called_once()
    dash_worker.create_process.assert_called_once()
    dash_worker._monitor_loop.assert_called_once()


def test_start_resume_stream(dash_worker, mocker):
    mocker.patch.object(dash_worker, "_load_state", return_value=(5, 120.0))  # Resume seq 5
    mocker.patch.object(dash_worker, "_cleanup")
    mocker.patch("os.makedirs")
    mocker.patch.object(dash_worker, "_setup_verification", return_value=(None, None))
    mocker.patch.object(dash_worker, "create_process", return_value=MagicMock())
    mocker.patch.object(dash_worker, "_monitor_loop")

    info = StreamInfoObject(url="http://test", key="key", start_time="100.0", stream_id="id")

    dash_worker.start(info)

    dash_worker._cleanup.assert_not_called()
    assert "Resuming from sequence 5"  # Log verification implied
    dash_worker._monitor_loop.assert_called_once()


def test_setup_verification_success(dash_worker, mocker):
    mocker.patch("glob.glob", return_value=["/tmp/frag-Frag1"])
    mocker.patch("shutil.move")
    mocker.patch("os.path.basename", return_value="frag-Frag1")

    info = StreamInfoObject(url="url", key="key")
    backup, target = dash_worker._setup_verification(info, "/tmp")

    assert backup == "/tmp/frag-Frag1.bak"
    assert target == "frag-Frag1"


def test_setup_verification_no_files(dash_worker, mocker):
    mocker.patch("glob.glob", return_value=[])
    info = StreamInfoObject(url="url", key="key")
    backup, target = dash_worker._setup_verification(info, "/tmp")
    assert backup is None


def test_verify_stream_continuity_success(dash_worker, mocker):
    # Mock loop waiting for file
    mocker.patch("time.time", side_effect=[0, 1])  # start wait
    # Assume file appears immediately
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch.object(dash_worker, "_is_content_identical", return_value=True)
    mocker.patch("os.remove")

    process = MagicMock()
    process.poll.return_value = None

    info = StreamInfoObject(url="url", key="key")
    dash_worker._verify_stream_continuity(info, "/tmp", "state", process, "backup", "target", 1, 100)

    os.remove.assert_called_with("backup")


def test_verify_stream_continuity_fail_reset(dash_worker, mocker):
    # File appears but content differs
    mocker.patch("time.time", side_effect=[0, 1, 2, 3, 4, 5])
    mocker.patch("os.path.exists", side_effect=[True, True])  # backup exists too
    mocker.patch.object(dash_worker, "_is_content_identical", return_value=False)
    mocker.patch.object(dash_worker, "_cleanup")
    mocker.patch.object(dash_worker, "_save_state")
    mocker.patch.object(dash_worker, "create_process", return_value=MagicMock())
    mocker.patch("os.makedirs")
    mocker.patch("os.remove")
    mocker.patch("shutil.move")

    process = MagicMock()
    process.poll.return_value = None

    info = StreamInfoObject(url="url", key="key", stream_id="id")
    seq, time_val, new_proc = dash_worker._verify_stream_continuity(info, "/tmp", "state", process, "backup", "target", 5, 100)

    assert seq == 0  # reset
    assert time_val == 100
    assert new_proc is not process
    process.terminate.assert_called()


def test_monitor_loop(dash_worker, mocker):
    # Mock glob to find files
    # First call find Frag1, second call find nothing (loop ends via stop_event)
    mocker.patch("glob.glob", side_effect=[["/tmp/Frag1"], []])
    mocker.patch("os.path.basename", return_value="Frag1")
    mocker.patch("re.search", return_value=MagicMock(group=lambda x: "1"))
    mocker.patch("os.path.getsize", return_value=100)

    # helper mocks
    mocker.patch.object(dash_worker, "_merge_fragments", return_value=True)
    mocker.patch.object(dash_worker, "_get_chunk_duration", return_value=6.0)
    mocker.patch.object(dash_worker, "_save_state")
    mocker.patch("builtins.open", mocker.mock_open(read_data=b"data"))
    mocker.patch("os.remove")

    info = StreamInfoObject(url="url", stream_id="b12", key="key", media_type=Media.AUDIO)
    process = MagicMock()
    process.poll.return_value = None

    dash_worker.stop_event.is_set.side_effect = [False, True]
    dash_worker.buffer_size_seconds = 6

    dash_worker._monitor_loop(info, "/tmp", "state", process, 0, 100.0)

    # Check queue put
    assert dash_worker.queue.put.called


def test_merge_fragments(dash_worker, mocker):
    mocker.patch("subprocess.run")
    assert dash_worker._merge_fragments(["f1", "f2"], "out") is True


def test_get_chunk_duration(dash_worker, mocker):
    mock_av = mocker.patch("av.open")
    container = MagicMock()
    stream = MagicMock()
    stream.duration = 100
    stream.time_base = 0.1
    container.streams.audio = []
    container.streams.video = [stream]
    mock_av.return_value.__enter__.return_value = container

    assert dash_worker._get_chunk_duration("file") == 10.0


def test_monitor_loop_stale_processing(dash_worker, mocker):
    # This test verifies that if a sequence is incomplete but stale, it is processed.
    # We simulate VIDEO mode where 2 fragments are required.
    # We only provide 1 fragment.
    # We advance time to trigger stale check.

    # Mock glob:
    # 1. First iteration: Returns Frag1 (incomplete)
    # 2. Second iteration: Returns Frag1 (still incomplete) - Time advanced -> should process
    # 3. Third iteration: No files -> stop loop via side effect
    mocker.patch("glob.glob", side_effect=[["/tmp/Frag1"], ["/tmp/Frag1"], []])
    mocker.patch("os.path.basename", return_value="Frag1")
    mocker.patch("re.search", return_value=MagicMock(group=lambda x: "1"))
    mocker.patch("os.path.getsize", return_value=100)

    # Simulate time passing:
    # 1. Init (seq start) = 100
    # 2. First loop check: 100 (elapsed 0)
    # 3. Second loop check: 120 (elapsed 20 > stale_threshold 10)
    mocker.patch("time.time", side_effect=[100, 100, 120, 120, 130])

    mocker.patch.object(dash_worker, "_is_complete_av", return_value=False)
    mocker.patch.object(dash_worker, "_merge_fragments", return_value=True)
    mocker.patch.object(dash_worker, "_get_chunk_duration", return_value=6.0)
    mocker.patch.object(dash_worker, "_save_state")
    mocker.patch("builtins.open", mocker.mock_open(read_data=b"data"))
    mocker.patch("os.remove")

    info = StreamInfoObject(url="url", key="key", media_type=Media.VIDEO)
    process = MagicMock()
    process.poll.return_value = None

    dash_worker.stop_event.is_set.side_effect = [False, False, True]
    dash_worker.buffer_size_seconds = 6
    dash_worker.stale_time_threshold = 10

    dash_worker._monitor_loop(info, "/tmp", "state", process, 0, 100.0)

    # Should have processed despite being incomplete because it was stale
    assert dash_worker.queue.put.called
