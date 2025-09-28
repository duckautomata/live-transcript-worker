# latest
Using version [1.5](#15-2025-09-28)

# Major version 1
Using version [1.5](#15-2025-09-28)

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
