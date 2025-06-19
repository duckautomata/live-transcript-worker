# latest
Using version [1.1](#11-2025-06-12)

# Major version 1
Using version [1.1](#11-2025-06-12)

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
