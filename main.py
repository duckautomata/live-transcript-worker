import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from live_transcript_worker.config import Config
from live_transcript_worker.status_reporter import StatusReporter
from live_transcript_worker.stream_watcher import StreamWatcher

# Logger will be used for all modules under live_transcript_worker
app_logger = logging.getLogger("live_transcript_worker")
project_root_dir = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(project_root_dir, "tmp", "_logs", "app.log")

# Rotation keeps disk usage bounded on long-running servers: at most
# LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1) across app.log and app.log.1..N
LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MB per file
LOG_BACKUP_COUNT = 50

shutdown_event = threading.Event()


def setup_logging():
    """Logs to both console and file. Console is info and up only. File is debug and up.

    The file is rotated once it reaches LOG_MAX_BYTES, keeping LOG_BACKUP_COUNT
    old files, so logs never grow unbounded while the server stays up.
    """
    app_logger.setLevel(logging.DEBUG)
    app_logger.propagate = False

    # Check if handlers are already configured to prevent duplicates if setup_logging is called multiple times
    if not app_logger.handlers:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        file_handler = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(file_formatter)
        app_logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter("%(levelname)-8s %(message)s")
        console_handler.setFormatter(console_formatter)
        app_logger.addHandler(console_handler)


def handle_args():
    argument = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    if argument.lower() == "-h" or argument.lower() == "--help":
        print("Usage: python main.py [config filename under config/ (default: config.yaml)]")
        print("Example: python main.py dev.yaml")
        exit(0)

    Config.config_filename = argument
    Config.get_config()  # Used to verify that we are able to load the config before doing anything else


def graceful_shutdown(signum, frame):
    """Signal handler to initiate a graceful shutdown."""
    signal_name = signal.Signals(signum).name
    app_logger.info(f"Received signal {signum} ({signal_name}). Initiating graceful shutdown.")
    shutdown_event.set()


def main():
    # Setting up listener to listen to signal events
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    app_version = os.getenv("APP_VERSION", "local")
    build_date = os.getenv("BUILD_DATE", "unknown")
    app_logger.info(f"Startup Version: {app_version}")
    app_logger.info(f"Built On: {build_date}")

    stream_watcher = StreamWatcher()
    incoming_mode = stream_watcher.incoming_polling_enabled
    if incoming_mode:
        app_logger.info("Incoming-queue mode enabled. Polling server's /incoming for URLs (streamers.urls is ignored)")
    streamers = Config.get_all_streamers_config()
    for streamer in streamers:
        key = streamer["key"]
        urls = streamer["urls"]
        active = streamer["active"]
        media_type = streamer["media_type"]
        app_logger.info(f"[{key}] loaded profile config. Active::{active} Media::{media_type}")
        if active:
            if incoming_mode:
                stream_watcher.add_incoming(key)
            else:
                stream_watcher.add(key, urls)

    stream_watcher.start()

    status_reporter = StatusReporter(shutdown_event)
    status_reporter.start()

    try:
        app_logger.info("Application started. Waiting for shutdown signal.")
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except Exception as e:
        app_logger.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        app_logger.info("Stopping all threads")
        stream_watcher.stop()


if __name__ == "__main__":
    handle_args()
    setup_logging()
    app_logger.info("========== SERVER START ==========")
    app_logger.info(f"Logging to rotating log file: '{log_path}'")
    main()
    app_logger.info("========== SERVER STOP ==========")
    print(f"log file can be found under: '{log_path}'", flush=True)
