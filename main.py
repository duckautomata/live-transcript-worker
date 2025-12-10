import os
import signal
import sys
import threading
import logging
from datetime import datetime

from src.live_transcript_worker.stream_watcher import StreamWatcher
from src.live_transcript_worker.config import Config

# Logger will be used for all modules under src/live_transcript_worker
app_logger = logging.getLogger("src.live_transcript_worker")
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
project_root_dir = os.path.dirname(os.path.abspath(__name__))
log_path = os.path.join(project_root_dir, "tmp", f"{timestamp}.log")

shutdown_event = threading.Event()

def setup_logging():
    """Logs to both console and file. Console is info and up only. File is debug and up.
    """
    app_logger.setLevel(logging.DEBUG)
    app_logger.propagate = False

    # Check if handlers are already configured to prevent duplicates if setup_logging is called multiple times
    if not app_logger.handlers:
        os.makedirs("tmp", exist_ok=True)
        file_handler = logging.FileHandler(log_path, mode="w")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(message)s",
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        app_logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter("%(levelname)-8s %(message)s")
        console_handler.setFormatter(console_formatter)
        app_logger.addHandler(console_handler)

def handle_args():
    if len(sys.argv) > 1:
        argument = sys.argv[1]
    else:
        argument = "config.yaml"

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
    streamers = Config.get_all_streamers_config()
    for streamer in streamers:
        key = streamer["key"]
        urls = streamer["urls"]
        active = streamer["active"]
        media_type = streamer["media_type"]
        app_logger.info(f"[{key}] loaded profile config. Active::{active} Media::{media_type}")
        if active:
            stream_watcher.add(key, urls)

    stream_watcher.start()

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
    app_logger.info(f"Creating log file for current run: '{log_path}'")
    main()
    app_logger.info("Goodbye")
    print(f"log file can be found under: '{log_path}'", flush=True)
