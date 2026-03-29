# live-transcript-worker
A Python program that watches for a stream to go live, then grabs and transcribes the audio.

## Overview

_System_

- **[Live Transcript System](#live-transcript-system)**
- **[Worker System](#worker-system)**
- **[Types of Workers](#types-of-workers)**

_Development_

- **[Tech Used](#tech-used)**
- **[Requirements](#requirements)**
- **[Running Source Code](#running-source-code)**
- **[Debugging/Logging](#debugginglogging)**

_Docker_
- **[Host Requirements](#host-requirements)**
- **[Version Guide](#version-guide)**
- **[Running with Docker](#running-with-docker)**

## System

### Live Transcript System
Live Transcript is a system that contains three programs:
- Worker: [live-transcript-worker](https://github.com/duckautomata/live-transcript-worker)
- Server: [live-transcript-server](https://github.com/duckautomata/live-transcript-server)
- Client: [live-transcript](https://github.com/duckautomata/live-transcript)

All three programs work together to transcribe a livestream for us to use in real-time.
- Worker (this) will process a livestream, transcribe the audio, and then upload the results to the server.
- Server acts as a cache layer between Worker and Client. It will store the current transcript. Once it receives a new transcript line, it will be broadcast to all connected clients.
- Client is the UI that renders the transcript for us to use.

### Worker System

The worker has three parts:
- watcher
- worker
- processor

The watcher will look at the URLs and wait until a livestream starts. Once it starts, it will tell the worker to start.

The worker will then receive a URL, start downloading the audio, and send chunks of a certain duration to the queue.

The processor will then take chunks off the queue, transcribe them, and then upload them to the server.

### Types of Workers
As of now, there is only one worker: MPEGFixedBitrateWorker. But there are more that will be added. Each worker has a set of pros and cons.

**MPEG-TS FixedBitrate**

This worker is the simplest. It reads a fixed number of bytes from the stream and then uses that as a chunk.

_Pros_
- Works everywhere. We just need to know what audio bitrate it is using
- Easiest to implement and verify that it works. Dead simple.
- If video ads are injected into the stream, this will detect it and ignore the data. Since we work with a specific bitrate.

_Cons_
- Audio only
- Least accurate tags. The worker does not take into account the live latency or if the stream stops in the middle. So, the timestamps could be way off.

**MPEG-TS Buffered**

Two parts: downloader and worker. Downloader will continuously add data to an internal buffer. Worker will then extract the data from the buffer every `n` seconds, resetting the buffer. Each extract is a chunk.

_Pros_
- Works with variable bitrate. Meaning it supports video and audio.
- Works anywhere.

_Cons_
- Cannot detect injected video ads.

**DASH**

Uses the MPEG-DASH standard instead of MPEG-TS. Used when we use `--live-from-start`.

This will use yt-dlp's built-in handling of DASH to create the fragment files for us. We will instead listen to when a new fragment file is created, read them in to be transcribed, and then keep track of what lines went to what fragments.

Since we are keeping track of every fragment from a stream, we can ensure perfect timestamp accuracy, even if the stream has a large latency or goes offline in the middle.

_Pros_
- Audio and Video, though audio only is preferred.
- Perfect timestamps
- can start from the very beginning, even if the worker starts late.

_Cons_
- Slower line updates compared to MPEG-TS
- yt-dlp supports very few websites with the --live-from-start argument.
- If it starts too late, it can take a while for it to catch up with live.


## Development

### Tech Used
- Python 3.12
- FFmpeg
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

### Requirements
- Python 3.11 or greater
- FFmpeg
- NVIDIA gpu to run the larger models. Or a decent cpu to run the smaller models.
- Linux or Windows, but a Linux system is preferred (pure Linux or WSL).

### Running Source Code

**NOTE**: This is only required to run the the source code. If you only want to run it and not develop it, then check out the [Docker seciton](#docker)

1. If you do not want to install any NVIDIA libraries, you can run the code in a dev container for development work.
2. If you do not want to use a dev containerm, then download and install the NVIDIA libraries
    - [cuBLAS for CUDA 12 via CUDA Toolkit](https://developer.nvidia.com/cuda-downloads)
        >_Note: There are other options for installing cuBLAS. You can find them [here](https://developer.nvidia.com/cublas). But the CUDA Toolkit is the easiest._
    - [cuDNN 9 for CUDA 12](https://developer.nvidia.com/cudnn)
    - **_Note_**: I use `cuda-toolkit-12-9` and `cudnn9-cuda-12-9` for my local development. You cannot use cuda-13 because it does not support cuda-13 at this moment.
3. Run `scripts/setup.sh`
4. Referencing `config/example.yaml`, create `config/config.yaml` and add your specific configurations.

When all of that is done, you can run `scripts/run.sh` to start live-transcript-worker.

If you wish to create more configuration files (example: dev.yaml), then you can specify what config to use by adding the name of the config file as the first argument. Example `scripts/run.sh dev` to use the dev.yaml config.

### Debugging/Logging

Logging is set up for the entire program, and everything should be logged. The console will print info and higher logs (everything but debug). On startup, a log file under `tmp/` will be created and will contain every log. In the event of an error, check this log file to see what went wrong.

### Updating Packages
```bash
uv lock --upgrade
uv sync
```

## Docker

### Host Requirements
- NVIDIA gpu to run the larger models. Or a decent cpu to run the smaller models.
- Install GPU drivers and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- Linux system. Either pure linux or WSL if you want to run it on a Windows computer.
- Docker

To verify that everyone is set up correctly, run the command `docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi` and it should print out the GPU information.

### Running with Docker
1. copy `docker-compose.yml`
2. create `config.yaml` from the example config file. Place it in the root dir where docker-compose.yml exists.
3. create the data directory
```bash
mkdir -p ./{tmp,models}
chmod -R 777 ./tmp ./models
```
4. Then start the container:
```bash
docker compose up -d
```

To update the container
```bash
docker compose pull && docker compose up -d && docker image prune -f
```

The models are not installed in the image. So, on the first start, it will download the model specified in the config file. However, any subsequent starts will reuse the model since the model folder `model/` is stored outside the container.

Logs and current state are stored in the `tmp/` folder outside the container. Because of this, state is not lost on restart.

**Note**: the docker container and the source code uses the same `tmp/` and `models/` folder to store runtime data. Because of this, it is required that you run either or, not both. If you want to run both development and a docker image, then use separate folders.
