import logging
import random
import time
from queue import Empty, Queue
from threading import Event, Thread

from live_transcript_worker.config import Config
from live_transcript_worker.custom_types import ProcessObject, StreamInfoObject
from live_transcript_worker.helper import StreamHelper
from live_transcript_worker.process_audio import ProcessAudio
from live_transcript_worker.storage import Storage
from live_transcript_worker.worker import Worker

logger = logging.getLogger(__name__)


class StreamWatcher:
    """
    Main live-transcript class.
    Handles waiting for a stream to start, downloads audio, transcribes audio, then uploads result to server.
    """

    # Number of consecutive "offline" stream-stats results that cause a URL to be
    # removed from the server's /incoming queue. Lets the worker resume cleanly
    # after a connection loss: a stream that has truly ended will read offline on
    # both checks and get cleaned up.
    INCOMING_OFFLINE_DELETE_THRESHOLD = 2

    def __init__(self):
        channel_polling = Config.get_server_config().get("channel_polling", {}) or {}
        self.retry_interval_seconds: int = channel_polling.get("interval_seconds", 60)
        self.max_retry_interval_seconds: int = channel_polling.get("max_interval_seconds", 9000)
        self.pre_scheduled_buffer_seconds: int = channel_polling.get("pre_scheduled_buffer_seconds", 300)

        incoming_polling = Config.get_server_config().get("incoming_polling", {}) or {}
        self.incoming_polling_enabled: bool = incoming_polling.get("enabled", False)
        self.incoming_poll_interval_seconds: int = incoming_polling.get("interval_seconds", 30)
        # Used to tell the threads when to stop. Used on shutdown.
        self.stop_event = Event()

        # Used to tell when the worker threads are done. Processing thread must not quit until all workers are done.
        self.worker_finished_event = Event()

        # Used to tell when the model is loaded and ready to start.
        self.ready_event = Event()

        # queue used to store audio chunks ready to be processed. Chunks need to be processed in order, but can be processed at any time.
        self.processing_queue: Queue[ProcessObject] = Queue()

        self.storage = Storage()
        self.process_thread = Thread(target=self.processor, daemon=True)
        self.watcher_threads: list[Thread] = []

    def add(self, key: str, urls: list[str]):
        """Creates a watcher for this key.

        Args:
            key (str): Key must match the servers key. Used to tell the server what channel this is for.
            urls (list[str]): List of urls to watch.
        """
        # We use a daemon do that it is automatically killed when the main program exits
        new_thread = Thread(target=self.watcher, args=((key, urls)), daemon=True)
        self.watcher_threads.append(new_thread)
        self.storage.create_paths(key)
        logger.debug(f"[{key}][add] successfully added thread")

    def add_incoming(self, key: str):
        """Creates an incoming-queue watcher for this key. Instead of polling a
        static URL list, the watcher polls the server's /incoming endpoint for
        URLs that the announcement bot has queued up.

        Args:
            key (str): Key must match the servers key.
        """
        new_thread = Thread(target=self.watcher_incoming, args=(key,), daemon=True)
        self.watcher_threads.append(new_thread)
        self.storage.create_paths(key)
        logger.debug(f"[{key}][add_incoming] successfully added thread")

    def start(self):
        """Starts all watcher threads."""
        if len(self.watcher_threads) == 0:
            logger.warning("No watchers created. Cannot start")
            return
        self.process_thread.start()

        self.ready_event.wait()
        logger.info("system is in a ready state. Starting threads")
        for thread in self.watcher_threads:
            thread.start()
            # need to make sure all threads are not in sync.
            time.sleep(1.2)

    def stop(self):
        """Stops all threads gracefully, then returns. Hard kills after 30s."""
        logger.info("Stopping StreamWatcher")
        self.stop_event.set()
        for thread in self.watcher_threads:
            if thread.is_alive():
                thread.join(timeout=5)
        self.worker_finished_event.set()
        if self.process_thread.is_alive():
            self.process_thread.join(timeout=30)
        self.storage.wait_for_uploads(timeout=30)

    def watcher(self, key: str, urls: list[str]):
        """
        Internal threaded method used to watch for when a stream starts.
        Once a stream starts, it will start the worker for that stream.
        Once the worker stops, it will start watching again.

        Each url has its own next_check time so a YouTube channel with a stream
        scheduled hours out can sleep without starving a Twitch url in the same
        key, which has no schedule and must be polled on the regular cadence.
        """
        logger.info(f"[{key}][watcher] starting thread")
        worker = Worker(key, self.processing_queue, self.stop_event)
        last_stream_id = ""
        next_url_checks: dict[str, float] = dict.fromkeys(urls, 0.0)
        while not self.stop_event.is_set():
            soonest = min(next_url_checks.values()) if next_url_checks else time.time()
            if time.time() < soonest:
                time.sleep(1)
                continue

            id_blacklist = Config.get_id_blacklist_config()
            for url in urls:
                if time.time() < next_url_checks[url]:
                    continue

                info: StreamInfoObject = StreamHelper.get_stream_stats_until_valid_start(url, 10, key)
                info.key = key
                info.media_type = StreamHelper.get_media_type(url, key)
                blacklisted = info.stream_id in id_blacklist

                if not blacklisted and info.is_live:
                    logger.info(
                        f'[{key}][watcher] stream "{info.stream_title}" id {info.stream_id} started at {info.start_time} using media {info.media_type}'
                    )
                    self.storage.activate(info=info)
                    last_stream_id = info.stream_id
                    worker.start(info)
                    self.storage.deactivate(key, info.stream_id)

                # Default to short retry; extend for scheduled or offline urls.
                now = time.time()
                next_url_checks[url] = now + self.retry_interval_seconds + random.randint(-5, 10)
                if not blacklisted and not info.is_live:
                    if info.scheduled_start_time > 0:
                        pre_stream = info.scheduled_start_time - self.pre_scheduled_buffer_seconds
                        next_url_checks[url] = max(next_url_checks[url], min(pre_stream, now + self.max_retry_interval_seconds))
                        logger.debug(
                            f"[{key}][watcher] {url} scheduled stream in {StreamHelper.format_duration(info.scheduled_start_time - now)}. "
                            f"Next check in {StreamHelper.format_duration(next_url_checks[url] - now)}."
                        )
                    elif info.confirmed_offline:
                        next_url_checks[url] = now + self.max_retry_interval_seconds
                        logger.debug(
                            f"[{key}][watcher] {url} offline with no schedule. "
                            f"Next check in {StreamHelper.format_duration(self.max_retry_interval_seconds)}."
                        )
                    elif "twitch.tv" not in url.lower():
                        logger.debug(
                            f"[{key}][watcher] {url} using default poll rate. "
                            f"Next check in {StreamHelper.format_duration(next_url_checks[url] - now)}."
                        )

                if self.stop_event.is_set():
                    logger.info(f"[{key}][watcher] stopping")
                    if not info.is_live:
                        self.storage.deactivate(key, info.stream_id)
                    return
            time.sleep(0.5)
        logger.info(f"[{key}][watcher] out of loop stopping. Using last_stream_id to deactivate.")
        self.storage.deactivate(key, last_stream_id)

    def watcher_incoming(self, key: str):
        """
        Watcher variant for incoming-queue mode. The URL list is fetched from the
        server's /incoming endpoint each `incoming_poll_interval_seconds`, instead
        of being a static list passed in at startup.

        URLs are removed from the server queue once they've been processed (after
        the stream ends and is deactivated) or once they've been confirmed offline
        INCOMING_OFFLINE_DELETE_THRESHOLD times in a row — the latter lets the
        worker recover when it loses connection mid-stream and the stream ends
        while the worker is offline.
        """
        logger.info(f"[{key}][watcher_incoming] starting thread")
        worker = Worker(key, self.processing_queue, self.stop_event)
        last_stream_id = ""
        # check time per URL; URLs at 0.0 are checked immediately on the next iteration.
        next_url_checks: dict[str, float] = {}
        # consecutive confirmed-offline counts per URL; reset on any non-offline result.
        offline_counts: dict[str, int] = {}
        next_incoming_poll = 0.0

        while not self.stop_event.is_set():
            # Refresh URL list from /incoming. New URLs are checked immediately.
            if time.time() >= next_incoming_poll:
                incoming_urls = self.storage.get_incoming_urls(key)
                for url in incoming_urls:
                    if url not in next_url_checks:
                        logger.info(f"[{key}][watcher_incoming] new incoming URL: {url}")
                        next_url_checks[url] = 0.0
                        offline_counts[url] = 0
                next_incoming_poll = time.time() + self.incoming_poll_interval_seconds

            # Sleep until either a URL is due for a check or the next /incoming poll.
            soonest_url_check = min(next_url_checks.values()) if next_url_checks else next_incoming_poll
            soonest = min(soonest_url_check, next_incoming_poll)
            if time.time() < soonest:
                time.sleep(1)
                continue

            id_blacklist = Config.get_id_blacklist_config()
            # snapshot keys since we may delete entries below.
            for url in list(next_url_checks.keys()):
                if time.time() < next_url_checks[url]:
                    continue

                info: StreamInfoObject = StreamHelper.get_stream_stats_until_valid_start(url, 10, key)
                info.key = key
                info.media_type = StreamHelper.get_media_type(url, key)
                blacklisted = info.stream_id in id_blacklist

                if not blacklisted and info.is_live:
                    logger.info(
                        f'[{key}][watcher_incoming] stream "{info.stream_title}" id {info.stream_id} started at {info.start_time} using media {info.media_type}'
                    )
                    self.storage.activate(info=info)
                    last_stream_id = info.stream_id
                    worker.start(info)
                    self.storage.deactivate(key, info.stream_id)

                # Track confirmed-offline checks. Scheduled streams (YouTube "begins
                # in X") aren't counted — they haven't started yet.
                offline_check = not info.is_live and info.scheduled_start_time == 0
                if offline_check and not blacklisted:
                    offline_counts[url] = offline_counts.get(url, 0) + 1
                    if offline_counts[url] >= self.INCOMING_OFFLINE_DELETE_THRESHOLD:
                        logger.info(f"[{key}][watcher_incoming] {url} confirmed offline {offline_counts[url]}x; removing from /incoming")
                        self.storage.delete_incoming_url(key, url)
                        next_url_checks.pop(url, None)
                        offline_counts.pop(url, None)
                        continue
                else:
                    offline_counts[url] = 0

                # Schedule next check. Same cadence as the URL-mode watcher, but we
                # don't extend offline checks out to max_retry_interval_seconds —
                # the URL will get cleaned up after the offline threshold instead.
                now = time.time()
                next_url_checks[url] = now + self.retry_interval_seconds + random.randint(-5, 10)
                if not blacklisted and not info.is_live and info.scheduled_start_time > 0:
                    pre_stream = info.scheduled_start_time - self.pre_scheduled_buffer_seconds
                    next_url_checks[url] = max(next_url_checks[url], min(pre_stream, now + self.max_retry_interval_seconds))
                    logger.debug(
                        f"[{key}][watcher_incoming] {url} scheduled stream in {StreamHelper.format_duration(info.scheduled_start_time - now)}. "
                        f"Next check in {StreamHelper.format_duration(next_url_checks[url] - now)}."
                    )

                if self.stop_event.is_set():
                    logger.info(f"[{key}][watcher_incoming] stopping")
                    if not info.is_live:
                        self.storage.deactivate(key, info.stream_id)
                    return
            time.sleep(0.5)
        logger.info(f"[{key}][watcher_incoming] out of loop stopping. Using last_stream_id to deactivate.")
        self.storage.deactivate(key, last_stream_id)

    def processor(self):
        """
        Internal threaded method used to pull chunks from the queue and send them to get processed.
        """
        logger.info("[processor] thread starting")
        audio_processor = ProcessAudio(self.ready_event)
        last_queue_item_time = time.time()
        while not self.stop_event.is_set() or not self.processing_queue.empty() or not self.worker_finished_event.is_set():
            try:
                item = self.processing_queue.get(timeout=0.5)
                last_queue_item_time = time.time()
                audio_processor.process_audio(item)
                self.processing_queue.task_done()
                if self.processing_queue.qsize() >= 10:
                    logger.warning(f"[processor] queue size is getting large: {self.processing_queue.qsize()} >= 10")
            except Empty:
                if time.time() - last_queue_item_time > 10 * 60:  # 10 minutes
                    # Model will only be unloaded once. So there is no harm in calling it multiple times.
                    audio_processor.unload_model()
                continue
            except Exception as e:
                logger.error(f"[processor] error in processing thread: {e}")
        del audio_processor
        logger.info("[processor] thread finished.")
