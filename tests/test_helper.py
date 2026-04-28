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
    assert info.scheduled_start_time == 0.0


def test_parse_upcoming_seconds_days():
    assert StreamHelper._parse_upcoming_seconds("ERROR: [youtube] DpNxmBaMB8Y: This live event will begin in 95 days.") == 95 * 86400


def test_parse_upcoming_seconds_combined():
    assert (
        StreamHelper._parse_upcoming_seconds("ERROR: [youtube] abc: This live event will begin in 1 day, 2 hours, 30 minutes, 5 seconds.")
        == 86400 + 2 * 3600 + 30 * 60 + 5
    )


def test_parse_upcoming_seconds_singular():
    assert StreamHelper._parse_upcoming_seconds("This live event will begin in 1 hour.") == 3600


def test_parse_upcoming_seconds_no_match():
    assert StreamHelper._parse_upcoming_seconds("ERROR: [youtube] some unrelated error") is None


def test_format_duration_examples_from_spec():
    # Examples from the request: 480s -> "8 minutes", 7890s -> "2 hours, 11 minutes, 30 seconds".
    assert StreamHelper.format_duration(480) == "8 minutes"
    assert StreamHelper.format_duration(7890) == "2 hours, 11 minutes, 30 seconds"


def test_format_duration_drops_zero_units_and_pluralizes():
    assert StreamHelper.format_duration(60) == "1 minute"
    assert StreamHelper.format_duration(3600) == "1 hour"
    assert StreamHelper.format_duration(86400) == "1 day"
    assert StreamHelper.format_duration(86400 + 3600 + 60 + 1) == "1 day, 1 hour, 1 minute, 1 second"
    assert StreamHelper.format_duration(7200) == "2 hours"


def test_format_duration_non_positive():
    assert StreamHelper.format_duration(0) == "0 seconds"
    assert StreamHelper.format_duration(-5) == "0 seconds"


def test_format_duration_truncates_fractional():
    # A float input is truncated to int seconds (we don't render sub-second precision).
    assert StreamHelper.format_duration(59.9) == "59 seconds"


def test_get_stream_stats_upcoming_via_stderr(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    mock_time = mocker.patch("time.time", return_value=1_000_000.0)
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = (
        "",
        "ERROR: [youtube] DpNxmBaMB8Y: This live event will begin in 5 hours.",
    )
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("https://www.youtube.com/channel/UC.../live")

    assert info.is_live is False
    assert info.scheduled_start_time == 1_000_000.0 + 5 * 3600
    assert info.confirmed_offline is False
    assert mock_time.called


def test_get_stream_stats_confirmed_offline_via_stderr(mocker):
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = (
        "",
        "ERROR: [youtube:tab] UC3n5uGu18FoCy23ggWWp8tA: The channel is not currently live",
    )
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("https://www.youtube.com/channel/UC.../live")

    assert info.is_live is False
    assert info.scheduled_start_time == 0.0
    assert info.confirmed_offline is True


def test_get_stream_stats_unknown_error_not_offline(mocker):
    """Other non-zero errors (e.g. member-only, network) should NOT trigger confirmed_offline."""
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = ("", "ERROR: Some unrelated network failure")
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("https://www.youtube.com/channel/UC.../live")

    assert info.is_live is False
    assert info.scheduled_start_time == 0.0
    assert info.confirmed_offline is False


def test_get_stream_stats_twitch_offline_uses_default_poll(mocker):
    """Real Twitch offline error from the wild. Three things must hold:
    1. scheduled_start_time / confirmed_offline stay at defaults (so the watcher
       uses the default poll rate, not the 2.5h max).
    2. No JSONDecodeError path is hit (empty stdout must not trigger json.loads).
    3. is_live stays False.
    """
    mock_popen = mocker.patch("subprocess.Popen")
    mock_logger_error = mocker.patch("live_transcript_worker.helper.logger.error")
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = (
        "",
        "ERROR: [twitch:stream] dokibird: The channel is not currently live\n",
    )
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("https://www.twitch.tv/dokibird")

    assert info.is_live is False
    assert info.scheduled_start_time == 0.0
    assert info.confirmed_offline is False
    # Regression: the Twitch error path must NOT fall through to json.loads("").
    json_errors = [c for c in mock_logger_error.call_args_list if "Could not decode JSON" in str(c)]
    assert not json_errors, f"unexpected JSON decode error: {json_errors}"


def test_get_stream_stats_twitch_skips_stderr_parsing(mocker):
    """Even if Twitch stderr happens to contain YouTube-style phrases, we must
    leave scheduled_start_time / confirmed_offline at defaults."""
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.returncode = 1
    process_mock.communicate.return_value = (
        "",
        "ERROR: This live event will begin in 5 hours. The channel is not currently live",
    )
    mock_popen.return_value = process_mock

    info = StreamHelper.get_stream_stats("https://www.twitch.tv/somechannel")

    assert info.is_live is False
    assert info.scheduled_start_time == 0.0
    assert info.confirmed_offline is False


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
