# Audio/video desync in LiveSegmentWorker (YouTube)

Investigated 2026-10-13 on the live densetsu-exe stream `9Mvg2efaRgo`. Production was running yt-dlp 2026.08.19 and ffmpeg 6.1.1 (from the image).

## Summary

On YouTube, the video in `LiveSegmentWorker` chunks lags the audio by a few seconds. The offset is constant for a run but differs from run to run. Production measured **2.99s** on this stream, and replicas measured between 0 and 2s. The source is in sync. The offset comes from how yt-dlp's internal ffmpeg merges YouTube's separate video and audio HLS streams. It resets each input's timestamps to zero independently, so the difference in where each input started becomes a permanent offset.

Neither our segmenter nor the server's `Remux`/`Trim` introduces the offset. Twitch isn't affected, because it delivers a single muxed stream.

## The pipeline

`LiveSegmentWorker` ([worker_segment_pipeline.py](../live_transcript_worker/worker_segment_pipeline.py)) runs:

```
yt-dlp -f b/bv+ba -S vcodec:h264,proto:m3u8,acodec:aac -o - URL
  | ffmpeg -i pipe:0 -c copy -f segment -segment_time 6 -reset_timestamps 1 ... chunk%06d.ts
```

On YouTube, yt-dlp always picks two separate HLS formats. Every YouTube run in `tmp/*/live_segment.log` shows `Downloading 1 format(s): 312+234`:

| Format | Content | Segment container |
|---|---|---|
| 312 | 1080p60 H.264 video | MPEG-TS |
| 234 | AAC audio | raw ADTS, timestamped with an ID3 tag per segment |

yt-dlp can't merge two formats to stdout itself, so it hands both playlists to its own ffmpeg. The command line of the running production process (`/proc/<pid>/cmdline`, headers trimmed) was:

```
ffmpeg -y -hide_banner -i <312 playlist> -i <234 playlist> \
       -c copy -map 0:0 -map 1:0 -f mpegts -loglevel warning -nostats -
```

There is no `-copyts`.

## Root cause

### 1. ffmpeg rebases each input independently

Without `-copyts`, ffmpeg subtracts each input file's own start time from that input's timestamps, so every input begins at 0. That's harmless when all streams come from one input. Here video and audio are two separate inputs, so each gets its own offset.

### 2. The two inputs start at different points in the stream

ffmpeg opens inputs one after another. It opens and probes the video playlist, which reads several seconds of live data, and only then opens the audio playlist. By then the live edge has moved on, so the audio starts later in the stream than the video.

Example from a run with `-loglevel verbose -debug_ts`:

```
Input #0 (video)  start: 7552.400000
Input #1 (audio)  start: 7555.401722      <- opened ~3s later in stream time

after rebasing:   video 7552.4 -> 0.0     audio 7555.4 -> 0.0
=> at any output time t, video shows stream time t+7552.4
                          audio plays stream time t+7555.4   (video 3s behind)
```

The production log for today's run shows the same pattern. Right after startup the video demuxer reported `skipping 3 segments ahead, expired from playlists`, meaning it sat idle for over 3s while the audio input was being opened. This stream is ultra-low-latency: 1s segments and a 3-segment playlist window.

### 3. A ~1s correction applied to the audio input only

At startup, the audio HLS demuxer emits timestamps that jump backwards by about one segment. In the example above, the timestamps ran 7555.40 → 7557.375 and then restarted at 7556.400. That's a quirk of reading ID3-timestamped ADTS segments several at a time. ffmpeg detects the backwards jump and shifts the audio input forward by ~1s to compensate:

```
[aist#1:0/aac] timestamp discontinuity (stream id=0): -998463, new offset= 998463
```

This line appears in every 312+234 run for mint and densetsu-exe; doki's 312+234 runs show no discontinuity line. The correction is per input, so only the audio moves, which cancels about 1s of the gap.

**Net offset = (audio start − video start) − ~1s.** Video is behind audio, fixed for the whole run, and re-rolled every time yt-dlp starts: stream start, the DASH→LiveSegment switch, or a restart.

### Why ~5s is common

*Not verified.* The offset is the time spent opening the video input, measured in whole segments, minus about 1s. The stream measured here used 1-second segments; doki's YouTube streams use 2-second segments (their URLs show `dur/2.000`), so a few seconds of startup can easily become a ~5s offset there.

### What is not the cause

- **The source:** video segment 8094 starts at 8095.400 and audio segment 8094 at 8095.405 (from its ID3 timestamp). YouTube's two playlists share one clock and publish segments at the same time (median difference 0.16s).
- **Our segmenter:** it has a single input, so any rebasing or correction shifts video and audio together.
- **The server's `Remux` / `Trim`:** same reason.

## Measurements

The offset was measured on real chunks by byte-matching, as described in the [appendix](#appendix-how-it-was-measured).

| Pipeline | Chunks | Video behind audio |
|---|---|---|
| Production output, today's run (chunks 1254–1260) | 7 | **+2.990s**, identical on every chunk |
| Replica, production image, run A | 15 | +1.002s on every chunk |
| Replica, production image, run B (start times only) | – | ~+1.0s (7680.400 / 7682.392, minus 1s) |
| Replica, host ffmpeg 7.1.5, two runs (start times only) | – | ~0.0s and +2.0s |
| With fix A below (`ffmpeg_i2:-copyts`), two runs | 9 + 14 | **0.000s** on every chunk except the first of each run (~1s) |

## Potential solutions

### A. Keep the original timestamps: `--downloader-args "ffmpeg_i2:-copyts"` (tested)

Add this to the yt-dlp command in `_create_ytdlp_process` ([worker_segment_pipeline.py:311](../live_transcript_worker/worker_segment_pipeline.py:311)):

```python
cmd += ["--downloader-args", "ffmpeg_i2:-copyts"]
```

`-copyts` makes ffmpeg keep the source timestamps. Video and audio already share a clock, so they stay aligned. yt-dlp only inserts `ffmpeg_i2` arguments in front of a *second* input, so single-stream downloads like Twitch are unaffected and keep ffmpeg's correction for timestamp jumps. The existing `ffmpeg:-loglevel warning -nostats` arguments still apply; yt-dlp merges both `--downloader-args` options. This was confirmed in yt-dlp's `-v` command-line output.

**Result:** every chunk after the first is in sync to 0.000s.

**Side effects, all measured:**

1. **Transcript timestamps shift ~1s later.**
   - **Today:** compared with YouTube's `PROGRAM-DATE-TIME`, timestamps are +1.61s late in the replica, and production's own logged timestamps were about +1.65s late.
   - **With the fix:** +2.66s late. Chunk-to-chunk variation went from ±0.48s to ±0.64s over 14 chunks.
   - **Why:** each chunk is stamped `seg_mtime - duration - live_latency` ([worker_live_segment.py:35](../live_transcript_worker/worker_live_segment.py:35)). The mtime is set when the chunk closes, and that waits until both streams reach the cut point. Video reaches ffmpeg ~1.6s later than audio for the same moment of the stream (median 3.49s vs 1.88s behind real time), even though YouTube publishes both together.
   - **Before vs after:** today, each chunk's audio is newer than its video (that's the desync), so the audio arrives last and sets the mtime. With the fix they cover the same moment, so the slower video sets it.
2. **The first two chunks of each run are off.** ffmpeg's correction now happens in our segmenter, which logged six audio corrections of 0.16s each. That makes it cut a 1.0s first chunk and a 5.0s second one. Both close during the startup burst, so their mtime-based timestamps are late: +7.4s and +3.6s, compared with +1.8s for chunk 0 today.
3. **Clips still start at 0.** Tested with the server's exact `Remux` and `Trim` commands. Chunks still start at 1.400s, the standard offset ffmpeg's MPEG-TS writer adds. The only difference is in the first chunk of a run, where video starts ~2s before audio (this is the true alignment):
   - `Remux` of a clip that includes it gives an mp4 whose audio starts at 1.985s, i.e. ~2s of silence at the start, plus 3 harmless "Non-monotonic DTS" warnings.
   - `Convert` (audio-only) of that clip gives a file starting at 1.96s. Add `-af asetpts=PTS-STARTPTS` if it must start at 0.
   - `Trim` always starts at 0, thanks to `-avoid_negative_ts make_zero`.
4. **Risk:** for YouTube, ffmpeg's correction for timestamp jumps now runs in our segmenter instead of in yt-dlp's ffmpeg. That handled the startup glitch correctly. A backwards jump in YouTube's timestamps mid-stream wasn't observed and wasn't tested.

**Optional add-on:** raise `live_latency_seconds` ([worker_abstract.py:48](../live_transcript_worker/worker_abstract.py:48)) by ~1s for `LiveSegmentWorker` to cancel side effect 1. That was tuned on one stream with 1s segments; the right value may depend on segment length (not measured).

### B. Align the input start times: `--downloader-args "ffmpeg_i2:-isync 0"` (not tested)

`-isync` is ffmpeg's built-in way to offset one input by the difference between the two inputs' start times. It keeps ffmpeg's per-input correction for timestamp jumps. But that per-input correction is what applies the ~1s audio-only shift in root-cause step 3. So based on ffmpeg's source, this would likely leave video ~1s *ahead* of audio. It's only worth trying if A's side effects turn out to be a problem.

### C. Timestamp chunks from the stream's own clock instead of file mtimes (larger change)

With A in place, YouTube's own timestamps reach our segmenter intact. The worker could stamp chunks from them instead of from when each file was written: anchor once per run, then advance by the stream's own clock. That would remove both A's ~1s shift and the ±0.5s chunk-to-chunk variation that exists today.

Trade-offs:
- It needs a robust anchor, e.g. the median of the mtime-based estimate over the first minute.
- It needs handling for upstream stalls and reconnects. The current per-chunk mtime anchoring was chosen deliberately so it heals itself after those (see the `LiveSegmentWorker` docstring).

### D. Run the merge ourselves (larger change)

Use yt-dlp only to resolve the playlist URLs (`yt-dlp -g`) and run our own ffmpeg with both inputs. That gives full control over timestamp options and probing, and could also expose YouTube's `PROGRAM-DATE-TIME` for exact timestamps. It means more code to own: auth arguments, headers, and URL expiry. URLs expire after ~6h, the same limit yt-dlp's ffmpeg has today.

### Not viable / mitigation only

- **Use a single muxed format:** the YouTube client that yt-dlp currently uses no longer offers combined audio+video HLS formats.
- **Probe less on the first input** (e.g. `ffmpeg_i1:-analyzeduration ...`): this might shrink the gap but can't remove it, and it wasn't tested.

## Implemented: YouTubeTimelineWorker

Option C was implemented as a separate worker rather than a change to `LiveSegmentWorker`: `live_transcript_worker/worker_youtube_timeline.py`, enabled per streamer with `use_youtube_timeline: true` (YouTube only, no fallback).

- It joins at the live edge, fetches the HLS segments itself, and reads each segment's clock from its own bytes: the ID3 timestamp of an audio segment, the PTS of a video segment.
- Video and audio are paired by sequence number and muxed per chunk with `-copyts`, so A/V sync is exact.
- Every line is stamped `start time + clock + 0.036 s`, so every line is VOD-accurate. That held against a finished VOD at 3 min, 17 min, 1 h 23 m and 3 h 03 m into a stream, across a stream break.
- `LiveSegmentWorker` and DASH are unchanged.

Validated on 2026-10-14 by running the worker in the production image against live 24/7 streams and byte-matching every chunk against YouTube's own segments:

| Stream | Run | Result |
|---|---|---|
| Lofi Girl, 12 days in (11 clock wraps), video | 60 chunks, with a 25 s freeze of the worker mid-run | A/V desync 0.0000 s; every line = start + clock + 0.036 s exactly; the freeze was caught up with no lost segments |
| Sky News, 48 days in (43 wraps), audio only | 24 chunks | every line exact |
| NASA | 18 chunks | its sequence numbers and clock had restarted days earlier, so start + clock pointed 4.4 h into the future; the worker detected this, stamped lines by arrival time and marked them not VOD-accurate |

## Recommendation

Ship **A**, optionally with the `live_latency_seconds` bump. It's a one-line change that removes a 1–5s A/V offset in every YouTube clip, at the cost of transcript timestamps landing ~1s later and two imprecise chunks per run. Consider **C** later if timestamp precision matters more than the jitter we already have.

## Out of scope

- **`DASHWorker`** also merges video+audio with ffmpeg without `-copyts`, but it does so one fragment pair at a time. Each pair shares a sequence number and should start together, so any per-fragment offset would be small. Not measured.
- **Normal-latency (5s-segment) streams** weren't available to measure. The "~5s" explanation and the size of A's timestamp shift on such streams are unverified.

## Appendix: how it was measured

- **A/V offset in real chunks:** with `-c copy`, every packet in a chunk is byte-identical to a packet in YouTube's source segments.
  1. Download the source segments while the pipeline runs.
  2. Hash every source packet. For video, use its MPEG-TS timestamp. For audio, use the segment's ID3 `com.apple.streaming.transportStreamTimestamp` + n × 1024 / 44100.
  3. Look each chunk packet up by hash.
  4. Compute *source time − chunk time* for each stream. It's constant per stream, and the difference between the two streams is the desync. Silent AAC frames repeat byte-for-byte, so ambiguous matches are skipped.
- **Transcript timestamp accuracy:** true time = the `PROGRAM-DATE-TIME` of the chunk's first audio sample. The mapping from source timestamps to wall clock was constant over 84 playlist fetches.
- **Replicas** ran in throwaway containers from the production image, next to the production worker without touching it:

  ```bash
  docker run --rm -v "$PWD/out:/out:z" --entrypoint bash duckautomata/live-transcript-worker:latest -c \
    'timeout -s INT 60 /app/bin/yt-dlp ... -o - URL 2>/out/ytdlp.log | ffmpeg ... /out/chunk%06d.ts'
  ```

  Put `timeout` *inside* the container. A bare `timeout docker run ...` does not stop it, because yt-dlp running as PID 1 ignores SIGTERM.
- **Log signatures** in `tmp/<key>/live_segment.log`: `Downloading 1 format(s): 312+234`, `timestamp discontinuity (stream id=0): -998463`, and `skipping N segments ahead, expired from playlists` near startup, where a large N means a larger start gap.
