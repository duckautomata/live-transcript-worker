from unittest.mock import MagicMock

import pytest

from src.live_transcript_worker.custom_types import Media, StreamInfoObject
from src.live_transcript_worker.stream_watcher import StreamWatcher


@pytest.fixture
def stream_watcher(mocker, mock_config, mock_storage):
    mocker.patch("src.live_transcript_worker.stream_watcher.Config", mock_config)
    mocker.patch("src.live_transcript_worker.stream_watcher.Storage", return_value=mock_storage)
    sw = StreamWatcher()
    sw.stop_event = MagicMock()
    return sw


def test_add(stream_watcher):
    stream_watcher.add("key", ["url1"])
    assert len(stream_watcher.watcher_threads) == 1


def test_start_stop(stream_watcher, mocker):
    stream_watcher.add("key", ["url"])
    mocker.patch("time.sleep")  # Should sleep 1.2
    mocker.patch.object(stream_watcher.process_thread, "start")
    for t in stream_watcher.watcher_threads:
        mocker.patch.object(t, "start")
        mocker.patch.object(t, "join")
        mocker.patch.object(t, "is_alive", return_value=True)  # for stop join

    stream_watcher.ready_event.set()
    stream_watcher.start()

    assert stream_watcher.process_thread.start.called

    stream_watcher.stop()
    assert stream_watcher.stop_event.is_set()


def test_watcher_loop(stream_watcher, mocker):
    # Mock checks
    mocker.patch("time.sleep")
    mocker.patch(
        "src.live_transcript_worker.helper.StreamHelper.get_stream_stats_until_valid_start",
        return_value=StreamInfoObject(is_live=True, stream_id="id", start_time="100"),
    )
    mocker.patch("src.live_transcript_worker.helper.StreamHelper.get_media_type", return_value=Media.AUDIO)

    mock_worker_cls = mocker.patch("src.live_transcript_worker.stream_watcher.Worker")
    mock_worker = mock_worker_cls.return_value

    stream_watcher.stop_event.is_set.side_effect = [False, True]

    stream_watcher.watcher("key", ["url"])

    mock_worker.start.assert_called()
    stream_watcher.storage.activate.assert_called()
    stream_watcher.storage.deactivate.assert_called()


def test_processor(stream_watcher, mocker):
    mocker.patch("src.live_transcript_worker.stream_watcher.ProcessAudio")

    item = MagicMock()
    stream_watcher.processing_queue.put(item)

    stream_watcher.stop_event.set()  # Stop immediately after processing
    # But loop condition is or not empty. So it will process item then stop?
    # condition: not stop OR not empty OR not finished
    # if stop is set, and empty is false (has item), it continues.
    # After get, item is processed. Then loop again.
    # We need to make queue empty eventually.

    # run in thread or just call method? Method is blocking loop.
    # We control loop with side effects or exceptions?
    # Let's rely on stop_event and queue state.

    # We need to ensure loop exits.
    # Loop: while not stop or not empty or not finished.
    # We set stop=True. Queue has 1 item.
    # Iter 1: not stop(False) -> True. Process item. Queue empty? Not yet properly.
    # Queue.get removes it. task_done called.

    stream_watcher.worker_finished_event.set()  # ensure finished is set too

    # We need to make sure `not empty` becomes False.

    stream_watcher.processor()

    assert stream_watcher.processing_queue.empty()
