# latest
Using version [2.3](#23-2026-02-05)

# Major version 2
Using version [2.3](#23-2026-02-05)

# Major version 1
Using version [1.10](#110-2025-12-26)

## 2.3 (2026-02-05)
- Added status reporter
- Renamed activeId to streamId and activeTitle to streamTitle

## 2.2 (2026-01-29)
- Updated yt-dlp deps.
- Added persistent http client to storage to reuse connections.

## 2.1 (2026-01-20)
**Important**
- Changed media upload url
- now send stream id with media so the server knows what stream it's for.

## 2.0 (2025-12-30)
**Important**
- Api has been changed. Media is now uploaded separately from transcript line. This means media will run in a separate thread to not block transcription.
- Added tests
- Change config structure

## 1.10 (2025-12-26)
**Important**
- Added ruff linting and formatting
- Added pyrefly type checking
- Fixed bug where restarting DASH Worker on the same stream would duplicate the last fragments in the buffer.
- Fixed bug that happened when dash/hsl sequence would restart at 1 when the stream died and restarted. We assumed none of the previous fragments would change. So it would do nothing until the new sequence caught up to the old sequence.

## 1.9 (2025-12-08)
**Important**
- Added staleness to fragments

## 1.8 (2025-11-30)
**Important**
- Changed creds to api key header

## 1.7 (2025-11-23)
**Important**
- Added DASHWorker for YouTube streams. Will start from the beginning and should allow for accurate timestamps.

## 1.6 (2025-11-11)
**Important**
- Added Deno to dockerfile since latest yt-dlp version requires it.

## 1.5 (2025-09-28)
**Important**
- Looped the stream stats until we get a valid start time. This should help prevent it from starting with a start time of None.

## 1.4 (2025-09-26)
**Important**
- Python 3.11+ is now required.

**Changes**
- Upgrading docker base iamge to cuda 12.9 and ubuntu 24
- Upgrading dependencies

## 1.3 (2025-08-05)
**Changes**
- Upgraded faster-whisper to 1.2.0
- Now able to use the newest model distil-large-v3.5 which is the most accurate model.

## 1.2 (2025-06-20)
**Changes**
- Whisper model will now be unloaded after 10 minutes of queue inactivity. Model will be reloaded when a new item is added to the queue.
    - Ideally, this should resolve a bug that occurs when the model is loaded for days at a time.
    - Model will still be loaded at startup. This is used to ensure that the model is downloaded before any other action occurs.
- Increased queue size warning log to 10 from 5.

## 1.1 (2025-06-12)
**Changes**
- Added MPEGBufferedWorker
- "video" media type now processes the video
- Added `id_blacklist` to the config file. Any id's in the list will be skipped
- Fixed `scripts/build.sh` to use sudo if docker requires it
- Fixed StreamHelper `get_duration` when the byte string metadata cannot be extracted
- Docker container now gracefully shuts down when container is stopped.

## 1.0 (2025-05-24)
Initial version. Currently only supports fixed bitrate worker.
