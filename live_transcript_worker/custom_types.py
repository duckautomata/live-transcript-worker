class Media:
    NONE = "none"
    AUDIO = "audio"
    VIDEO = "video"


class ProcessObject:
    """Object type used to hold data ready to be processed"""

    raw: bytes | None
    audio_start_time: float
    key: str
    media_type: str

    def __init__(self, raw: bytes | None, audio_start_time: float, key: str, media_type: str):
        self.raw = raw
        self.audio_start_time = audio_start_time
        self.key = key
        self.media_type = media_type


class MediaUploadObject:
    """Object type used to hold data ready to be uploaded"""

    key: str
    stream_id: str
    id: int
    path: str

    def __init__(self, key: str, stream_id: str, id: int, path: str):
        self.key = key
        self.stream_id = stream_id
        self.id = id
        self.path = path

    def __eq__(self, other):
        if not isinstance(other, MediaUploadObject):
            return NotImplemented
        return self.key == other.key and self.stream_id == other.stream_id and self.id == other.id and self.path == other.path


class StreamInfoObject:
    """Object type used to hold metadata about a stream"""

    url: str
    is_live: bool
    stream_id: str
    stream_title: str
    start_time: str
    key: str
    media_type: str

    def __init__(
        self,
        url: str = "",
        is_live: bool = False,
        stream_id: str = "Unknown ID",
        stream_title: str = "Unknown Title",
        start_time: str = "0",
        key: str = "",
        media_type: str = Media.NONE,
    ):
        self.url = url
        self.is_live = is_live
        self.stream_id = stream_id
        self.stream_title = stream_title
        self.start_time = start_time
        self.key = key
        self.media_type = media_type
