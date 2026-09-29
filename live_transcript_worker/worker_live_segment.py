from live_transcript_worker.custom_types import StreamInfoObject
from live_transcript_worker.worker_segment_pipeline import SegmentPipelineWorker


class LiveSegmentWorker(SegmentPipelineWorker):
    """
    General-purpose live stream worker for any platform supported by yt-dlp.

    yt-dlp pipes the stream to ffmpeg, which writes keyframe-aligned MPEG-TS
    segments to a directory. Each segment is independently decodable and has
    accurate duration metadata. See SegmentPipelineWorker for the pipeline,
    its diagnostics (tmp/{key}/live_segment.log) and its process handling.

    Unlike TwitchLFSWorker, this worker joins the stream at the live edge
    (no --live-from-start), so there is no complete buffer of the full stream.
    Timestamps are approximated from each segment file's modification time:
    ffmpeg stamps a segment when it finishes writing it, so the mtime marks when
    that content was ingested, regardless of when this worker reads the file.
    Each segment is anchored independently to its own production time
    (mtime - duration - live_latency). This stays correct even when the monitor
    drains a backlog faster than real time, and self-heals after an upstream
    stall or reconnect instead of drifting permanently behind live.

    Supports VIDEO (video+audio mux) or AUDIO-only streams depending on
    info.media_type.
    """

    _NAME = "LiveSegmentWorker"
    _SEGMENT_SUBDIR = "live_segments"
    _LOG_FILENAME = "live_segment.log"
    _VOD_ACCURATE = False
    # Joins at the live edge, so a fresh yt-dlp/ffmpeg pair loses nothing already sent.
    _RESTART_ON_UNUSABLE = True

    def _timestamp_segment(self, info: StreamInfoObject, seg_mtime: float, duration: float) -> float:
        return seg_mtime - duration - self.live_latency_seconds
