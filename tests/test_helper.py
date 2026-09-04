import os
from unittest.mock import MagicMock

import pytest

from live_transcript_worker.custom_types import Media
from live_transcript_worker.helper import StreamHelper

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio")


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


def test_ytdlp_format_args_prefer_h264_aac_without_codec_filters():
    """Regression for "Requested format is not available": no [codec] filters
    anywhere, the H.264/AAC preference is a sort order only."""
    sort = ["-S", "vcodec:h264,proto:m3u8,acodec:aac"]
    assert StreamHelper.ytdlp_format_args(Media.VIDEO, to_stdout=True) == ["-f", "b/bv+ba", *sort]
    assert StreamHelper.ytdlp_format_args(Media.VIDEO, to_stdout=False) == ["-f", "bv*+ba/b", *sort]
    for media_type in (Media.AUDIO, Media.NONE):
        assert StreamHelper.ytdlp_format_args(media_type, to_stdout=True) == ["-f", "ba/b", *sort]
        assert StreamHelper.ytdlp_format_args(media_type, to_stdout=False) == ["-f", "ba/b", *sort]
    for args in (StreamHelper.ytdlp_format_args(Media.VIDEO, True), StreamHelper.ytdlp_format_args(Media.AUDIO, False)):
        assert "[" not in args[args.index("-f") + 1]


def test_trim_log_file_keeps_tail_once_over_limit(tmp_path):
    path = tmp_path / "x.log"
    original = b"".join(f"line {i:04d}\n".encode() for i in range(100))
    path.write_bytes(original)

    StreamHelper.trim_log_file(str(path), max_bytes=500, keep_bytes=100)

    data = path.read_bytes()
    assert data.startswith(b"--- truncated earlier history ---\n")
    assert data.endswith(original[-100:])
    assert len(data) < 500


def test_trim_log_file_noop_when_small_or_missing(tmp_path):
    path = tmp_path / "small.log"
    path.write_bytes(b"short\n")

    StreamHelper.trim_log_file(str(path), max_bytes=500, keep_bytes=100)
    assert path.read_bytes() == b"short\n"

    # Missing file must not raise.
    StreamHelper.trim_log_file(str(tmp_path / "missing.log"))


def test_open_process_log_creates_dir_and_stamps_header(tmp_path):
    path = tmp_path / "key" / "live_segment.log"

    with StreamHelper.open_process_log(str(path), "LiveSegmentWorker", "abc123") as log_file:
        assert not log_file.closed
        log_file.write("child output\n")

    text = path.read_text()
    assert "--- LiveSegmentWorker started at " in text
    assert "(stream: abc123) ---\nchild output\n" in text

    # A second run appends after the first.
    with StreamHelper.open_process_log(str(path), "LiveSegmentWorker", "def456"):
        pass
    assert path.read_text().count("--- LiveSegmentWorker started at ") == 2


def test_open_process_log_trims_oversized_log_before_appending(tmp_path):
    path = tmp_path / "ytdlp.log"
    path.write_bytes(b"x" * 1_200_000)

    with StreamHelper.open_process_log(str(path), "yt-dlp (DASH)", "abc"):
        pass

    data = path.read_bytes()
    assert data.startswith(b"--- truncated earlier history ---\n")
    assert len(data) < 300_000
    assert b"--- yt-dlp (DASH) started at " in data[-200:]


def test_describe_codecs_real_media():
    with open(os.path.join(AUDIO_DIR, "test-1.mp3"), "rb") as f:
        summary = StreamHelper.describe_codecs(f.read())

    assert summary.startswith("audio=")
    assert "mp3" in summary


def test_describe_codecs_never_raises():
    assert StreamHelper.describe_codecs(b"not media at all").startswith("unreadable")
    assert StreamHelper.describe_codecs(b"").startswith("unreadable")


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
