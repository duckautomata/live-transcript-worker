from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media
from live_transcript_worker.helper import StreamHelper


@pytest.fixture(autouse=True)
def _stub_config(mocker):
    """Stub Config so ytdlp_auth_args() doesn't require a real config.yaml."""
    mock_conf = mocker.patch("live_transcript_worker.helper.Config")
    mock_conf.get_server_config.return_value = {}
    mock_conf.get_streamer_config.return_value = {}
    return mock_conf


def test_remove_date():
    assert StreamHelper.remove_date("Stream Title 2023-01-01") == "Stream Title"
    assert StreamHelper.remove_date("2023-01-01 Stream Title") == "Stream Title"
    assert StreamHelper.remove_date("Stream 12/12/2023 Title") == "Stream  Title"
    assert StreamHelper.remove_date("Title 12:00") == "Title"
    assert StreamHelper.remove_date("Clean Title") == "Clean Title"


def test_ytdlp_auth_args_twitch_returns_empty(_stub_config):
    assert StreamHelper.ytdlp_auth_args("https://www.twitch.tv/foo") == []


def test_ytdlp_auth_args_youtube_default(_stub_config):
    args = StreamHelper.ytdlp_auth_args("https://www.youtube.com/live/abc")
    assert "--match-filter" in args
    assert "--cookies" not in args
    assert "--plugin-dirs" not in args
    assert "--extractor-args" not in args


def test_ytdlp_auth_args_pot_provider_enabled(_stub_config):
    _stub_config.get_server_config.return_value = {
        "pot_provider": {
            "enabled": True,
            "url": "http://bgutil-provider:4416",
            "plugin_dir": "/app/yt-dlp-plugins",
        }
    }
    args = StreamHelper.ytdlp_auth_args("https://www.youtube.com/live/abc")
    assert "--plugin-dirs" in args
    assert "/app/yt-dlp-plugins" in args
    assert "--extractor-args" in args
    assert "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416" in args


def test_ytdlp_auth_args_pot_provider_disabled(_stub_config):
    _stub_config.get_server_config.return_value = {"pot_provider": {"enabled": False}}
    args = StreamHelper.ytdlp_auth_args("https://www.youtube.com/live/abc")
    assert "--plugin-dirs" not in args
    assert "--extractor-args" not in args


def test_ytdlp_auth_args_pot_provider_skipped_for_twitch(_stub_config):
    _stub_config.get_server_config.return_value = {"pot_provider": {"enabled": True}}
    assert StreamHelper.ytdlp_auth_args("https://www.twitch.tv/foo") == []


def test_get_stream_stats_success(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.communicate.return_value = (
        '{"is_live": true, "id": "123", "title": "Test Title", "release_timestamp": 12345}',
        "",
    )
    process_mock.returncode = 0
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("http://test.com")

    assert info.is_live is True
    assert info.stream_id == "123"
    assert info.stream_title == "Test Title"
    assert info.start_time == "12345"


def test_get_stream_stats_twitch(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    # Twitch uses 'timestamp', 'display_id', 'description'
    process_mock.communicate.return_value = (
        '{"is_live": true, "id": "123", "display_id": "User", "description": "Desc", "timestamp": 12345}',
        "",
    )
    process_mock.returncode = 0
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("http://twitch.tv/user")

    assert info.is_live is True
    assert "User - Desc" in info.stream_title
    assert info.start_time == "12345"


def test_get_stream_stats_failure(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = ("", "Error")
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("http://test.com")
    assert info.is_live is False


def test_get_stream_stats_json_error(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.communicate.return_value = ("invalid json", "")
    process_mock.returncode = 0
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("http://test.com")
    assert info.is_live is False


def test_get_stream_stats_until_valid_start_immediate(mocker):
    mocker.patch.object(
        StreamHelper,
        "get_stream_stats",
        return_value=MagicMock(is_live=True, start_time="12345"),
    )
    info = StreamHelper.get_stream_stats_until_valid_start("url", 5)
    assert info.start_time == "12345"


def test_get_stream_stats_until_valid_start_retry(mocker):
    # First call invalid start time, second call valid
    bad_info = MagicMock(is_live=True, start_time="0")
    good_info = MagicMock(is_live=True, start_time="12345")

    mocker.patch.object(StreamHelper, "get_stream_stats", side_effect=[bad_info, good_info])
    mocker.patch("time.sleep")  # speed up test

    info = StreamHelper.get_stream_stats_until_valid_start("url", 2)
    assert info.start_time == "12345"
    assert StreamHelper.get_stream_stats.call_count == 2


def test_get_stream_stats_until_valid_start_not_live(mocker):
    mocker.patch.object(StreamHelper, "get_stream_stats", return_value=MagicMock(is_live=False))
    info = StreamHelper.get_stream_stats_until_valid_start("url", 5)
    assert info.is_live is False


def test_get_duration_valid(mocker):
    # Mock av.open to return a container with duration
    mock_av = mocker.patch("av.open")
    mock_container = MagicMock()
    mock_container.duration = 10_000_000  # 10 seconds in microseconds
    mock_container.start_time = 0
    mock_av.return_value.__enter__.return_value = mock_container

    duration = StreamHelper.get_duration(b"fake_audio")
    assert duration == 10.0


def test_get_duration_error(mocker):
    mocker.patch("av.open", side_effect=Exception("av error"))
    assert StreamHelper.get_duration(b"bad") == 0.0


def test_get_media_type(mocker):
    mock_config = mocker.patch("live_transcript_worker.helper.Config")
    mock_config.get_streamer_config.return_value = {"media_type": Media.VIDEO}
    assert StreamHelper.get_media_type("http://youtube.com", "key") == Media.VIDEO

    # Twitch does not override
    assert StreamHelper.get_media_type("http://twitch.tv", "key") == Media.VIDEO


def test_get_stream_stats_none_start_time(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    # release_timestamp is None, timestamp is None
    process_mock.communicate.return_value = (
        '{"is_live": true, "id": "123", "title": "Test Title", "release_timestamp": null, "timestamp": null}',
        "",
    )
    process_mock.returncode = 0
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("http://test.com")

    assert info.is_live is True
    assert info.start_time != "None"
    # Should be a numeric string (fallback to time.time())
    assert float(info.start_time) > 0
