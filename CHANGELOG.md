# latest
Using version [1.2](#12-2025-06-20)

# Major version 1
Using version [1.2](#12-2025-06-20)

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
