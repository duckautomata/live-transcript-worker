from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media, StreamInfoObject
from live_transcript_worker.worker_dash import DASHWorker

MODULE = "live_transcript_worker.worker_dash"


@pytest.fixture
def dash_worker(mocker):
    mocker.patch("live_transcript_worker.worker_abstract.Config")
    helper_config = mocker.patch("live_transcript_worker.helper.Config")
    helper_config.get_server_config.return_value = {}
    return DASHWorker("key", MagicMock(), MagicMock())


def _info(media_type):
    return StreamInfoObject(url="https://www.youtube.com/watch?v=abc", key="key", stream_id="abc", media_type=media_type)


def test_create_process_uses_filter_free_selector_and_logs_to_ytdlp_log(dash_worker, mocker, tmp_path):
    """Same regression as LiveSegmentWorker: a [codec] filter fails the whole
    download when yt-dlp reports a codec as unknown, so the DASH worker also
    expresses its H.264/AAC preference as a sort order."""
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    fragment_dir = tmp_path / "fragments"
    fragment_dir.mkdir()

    proc = dash_worker.create_process(_info(Media.VIDEO), str(fragment_dir))

    assert proc is popen.return_value
    cmd = popen.call_args.args[0]
    assert "--live-from-start" in cmd
    fmt = cmd[cmd.index("-f") + 1]
    assert fmt == "bv*+ba/b"
    assert "[" not in fmt
    assert cmd[cmd.index("-S") + 1] == "vcodec:h264,proto:m3u8,acodec:aac"
    assert cmd[cmd.index("-o") + 1] == f"{fragment_dir}/%(id)s.%(format_id)s"
    # yt-dlp's output goes to the per-key ytdlp.log, stamped with a run header.
    kwargs = popen.call_args.kwargs
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"].closed  # parent's copy closed after spawning; the child keeps its fd
    log_text = (tmp_path / "ytdlp.log").read_text()
    assert "--- yt-dlp (DASH) started at " in log_text
    assert "(stream: abc) ---" in log_text


def test_create_process_audio_selector(dash_worker, mocker, tmp_path):
    popen = mocker.patch(f"{MODULE}.subprocess.Popen")
    fragment_dir = tmp_path / "fragments"
    fragment_dir.mkdir()

    for media_type in (Media.AUDIO, Media.NONE):
        dash_worker.create_process(_info(media_type), str(fragment_dir))
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("-f") + 1] == "ba/b"
        assert cmd[cmd.index("-S") + 1] == "vcodec:h264,proto:m3u8,acodec:aac"
