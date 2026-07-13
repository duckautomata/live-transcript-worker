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


class _CompositeStopEvent:
    """Event-like wrapper that reports is_set() True if any of its component
    events are set. Workers (and their downloader threads) only call is_set() on
    their stop_event, so we don't need to implement set/clear/wait — that lets us
    OR the global stop_event with a per-key restart_event without touching the
    worker classes.
    """

    def __init__(self, *events: Event):
        self._events: tuple[Event, ...] = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


class StreamWatcher:
    """
    Main live-transcript class.
    Handles waiting for a stream to start, downloads audio, transcribes audio, then uploads result to server.
    """

    def __init__(self):
        channel_polling = Config.get_server_config().get("channel_polling", {}) or {}
        self.retry_interval_seconds: int = channel_polling.get("interval_seconds", 60)
        self.max_retry_interval_seconds: int = channel_polling.get("max_interval_seconds", 9000)
        self.pre_scheduled_buffer_seconds: int = channel_polling.get("pre_scheduled_buffer_seconds", 300)

        incoming_polling = Config.get_server_config().get("incoming_polling", {}) or {}
        self.incoming_polling_enabled: bool = incoming_polling.get("enabled", False)
        self.incoming_poll_interval_seconds: int = incoming_polling.get("interval_seconds", 30)

        # Long-poll notifications. When enabled, a single background thread
        # holds a GET /events request open against the server and fans the
        # returned signals out to the per-key events below, so new-stream and
        # restart signals land in ~a second instead of a poll interval. The
        # plain interval polls remain as a slow safety net.
        events_polling = Config.get_server_config().get("events_polling", {}) or {}
        self.events_enabled: bool = events_polling.get("enabled", True)
        self.events_wait_seconds: int = events_polling.get("wait_seconds", 25)
        self.events_fallback_interval_seconds: int = events_polling.get("fallback_interval_seconds", 300)
        # Number of consecutive "offline" stream-stats results that cause a URL to be
        # removed from the server's /incoming queue. Lets the worker resume cleanly
        # after a connection loss: a stream that has truly ended will read offline on
        # the configured number of checks and get cleaned up.
        self.incoming_offline_delete_threshold: int = incoming_polling.get("offline_delete_threshold", 2)
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

        # Per-key restart events. Set by the events listener (or the legacy
        # restart-poller thread) when the operator POSTs to /{key}/restart on
        # the server, and read by the watcher (via _CompositeStopEvent) so the
        # running Worker aborts. Pre-allocated by add()/add_incoming() so tests
        # can inject one before calling the watcher method directly.
        self._restart_events: dict[str, Event] = {}

        # Per-key "check /incoming now" nudges, set by the events listener and
        # consumed by watcher_incoming. Pre-allocated by add_incoming().
        self._incoming_events: dict[str, Event] = {}

    def add(self, key: str, urls: list[str]):
        """Creates a watcher for this key.

        Args:
            key (str): Key must match the servers key. Used to tell the server what channel this is for.
            urls (list[str]): List of urls to watch.
        """
        # We use a daemon do that it is automatically killed when the main program exits
        self._restart_events.setdefault(key, Event())
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
        self._restart_events.setdefault(key, Event())
        self._incoming_events.setdefault(key, Event())
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
        if self.events_enabled:
            keys = sorted(self._restart_events.keys())
            Thread(target=self._events_listener, args=(keys,), daemon=True).start()
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

        Also runs a background restart poller to detect when the server wants the worker to restart.
        """
        logger.info(f"[{key}][watcher] starting thread")
        restart_event = self._restart_events.setdefault(key, Event())
        composite_stop = _CompositeStopEvent(self.stop_event, restart_event)
        worker = Worker(key, self.processing_queue, composite_stop)
        if not self.events_enabled:
            Thread(target=self._restart_poller, args=(key, restart_event), daemon=True).start()

        last_stream_id = ""
        next_url_checks: dict[str, float] = dict.fromkeys(urls, 0.0)
        while not self.stop_event.is_set():
            if restart_event.is_set():
                self._handle_restart(key, last_stream_id, restart_event)
                last_stream_id = ""
                # Re-check every URL immediately on the next iteration.
                next_url_checks = dict.fromkeys(urls, 0.0)
                continue

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

                    # If the worker exited because of a restart request, bail out
                    # of this for-loop so the while-top can reset state cleanly.
                    if restart_event.is_set():
                        break

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
        incoming_offline_delete_threshold times in a row — the latter lets the
        worker recover when it loses connection mid-stream and the stream ends
        while the worker is offline.

        Also runs a background restart poller to detect when the server wants the worker to restart.
        """
        logger.info(f"[{key}][watcher_incoming] starting thread")
        restart_event = self._restart_events.setdefault(key, Event())
        incoming_event = self._incoming_events.setdefault(key, Event())
        composite_stop = _CompositeStopEvent(self.stop_event, restart_event)
        worker = Worker(key, self.processing_queue, composite_stop)
        if not self.events_enabled:
            Thread(target=self._restart_poller, args=(key, restart_event), daemon=True).start()

        # With events polling the listener nudges us via incoming_event the
        # moment a URL is queued, so the interval refresh is just a safety net.
        incoming_interval = self.events_fallback_interval_seconds if self.events_enabled else self.incoming_poll_interval_seconds

        last_stream_id = ""
        # check time per URL; URLs at 0.0 are checked immediately on the next iteration.
        next_url_checks: dict[str, float] = {}
        # consecutive confirmed-offline counts per URL; reset on any non-offline result.
        offline_counts: dict[str, int] = {}
        next_incoming_poll = 0.0

        while not self.stop_event.is_set():
            if restart_event.is_set():
                self._handle_restart(key, last_stream_id, restart_event)
                last_stream_id = ""
                next_url_checks.clear()
                offline_counts.clear()
                next_incoming_poll = 0.0
                continue

            # Refresh URL list from /incoming, either on a nudge from the
            # events listener or when the interval elapses. New URLs are
            # checked immediately.
            if incoming_event.is_set() or time.time() >= next_incoming_poll:
                incoming_event.clear()
                incoming_urls = self.storage.get_incoming_urls(key)
                for url in incoming_urls:
                    if url not in next_url_checks:
                        logger.info(f"[{key}][watcher_incoming] new incoming URL: {url}")
                        next_url_checks[url] = 0.0
                        offline_counts[url] = 0
                next_incoming_poll = time.time() + incoming_interval

            # Sleep until a URL is due for a check or the next /incoming poll.
            # The 1s tick also bounds how long an events-listener nudge waits
            # before the top-of-loop check picks it up.
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

                    # If the worker exited because of a restart request, bail out
                    # of this for-loop so the while-top can reset state cleanly.
                    if restart_event.is_set():
                        break

                # Track confirmed-offline checks. Scheduled streams (YouTube "begins
                # in X") aren't counted — they haven't started yet.
                offline_check = not info.is_live and info.scheduled_start_time == 0
                if offline_check and not blacklisted:
                    offline_counts[url] = offline_counts.get(url, 0) + 1
                    if offline_counts[url] >= self.incoming_offline_delete_threshold:
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

    def _handle_restart(self, key: str, last_stream_id: str, restart_event: Event) -> None:
        """Resets the watcher's per-key server state after a restart was signaled.
        Called from the watcher loop's top-of-iteration check, once the running
        Worker (if any) has already been aborted via the composite stop event and
        the bg poller has cleared the server-side request.
        """
        logger.info(f"[{key}][watcher] handling restart request")
        if last_stream_id:
            self.storage.deactivate(key, last_stream_id)
        restart_event.clear()

    def _check_restart(self, key: str, restart_event: Event) -> None:
        """Single restart check + ack for one key: GETs the server's restart
        flag and, when pending, sets restart_event (which causes the running
        Worker to exit via the composite stop event) and DELETEs the
        server-side request so it isn't re-handled. The watcher main loop is
        responsible for clearing restart_event after it has reset its state.

        Skips while restart_event is already set — that means a previous
        request is still being handled, so we shouldn't trigger again or wipe
        a fresh POST.
        """
        try:
            if not restart_event.is_set() and self.storage.is_restart_requested(key):
                logger.info(f"[{key}][restart_poller] restart requested — aborting current stream")
                restart_event.set()
                self.storage.delete_restart_request(key)
        except Exception as e:
            logger.error(f"[{key}][restart_poller] error: {e}")

    def _restart_poller(self, key: str, restart_event: Event) -> None:
        """Legacy fallback thread (events_polling.enabled: false): polls the
        server's /{key}/restart endpoint at the same cadence as /incoming.
        When events polling is enabled, _events_listener performs the same
        check the moment the server reports a restart signal instead.
        """
        interval = self.incoming_poll_interval_seconds
        logger.debug(f"[{key}][restart_poller] starting (interval={interval}s)")
        while not self.stop_event.is_set():
            self._check_restart(key, restart_event)
            # Sleep with early-exit on stop. Event.wait returns True if the event
            # was set (i.e. shutdown), in which case we exit the poller.
            if self.stop_event.wait(interval):
                return
        logger.debug(f"[{key}][restart_poller] stopping")

    def _events_listener(self, keys: list[str]) -> None:
        """Background thread: holds a single long-poll GET /events covering
        every key and fans the returned flags out to the same in-process
        signals the interval pollers use — "restart" runs a restart check +
        ack, "incoming" nudges that key's watcher_incoming to refresh its URL
        list. The server answers the moment a signal is posted, which is what
        gets new-stream/restart latency down from a poll interval to ~a second.

        When the long poll fails (server without /events, network error), the
        round degrades to one legacy-cadence poll: check /restart for every
        key and nudge every incoming watcher to refresh — exactly the old
        interval-polling behaviour — then retry /events on the next round.
        """
        cursor = 0
        logger.info(f"[events_listener] starting (keys={keys}, wait={self.events_wait_seconds}s)")
        while not self.stop_event.is_set():
            result = self.storage.poll_events(keys, cursor, self.events_wait_seconds)

            if result is None:
                # Degraded round: behave like the legacy interval pollers.
                for key in keys:
                    self._check_restart(key, self._restart_events[key])
                for event in self._incoming_events.values():
                    event.set()
                if self.stop_event.wait(self.incoming_poll_interval_seconds):
                    break
                continue

            events, cursor = result
            for key, flags in events.items():
                if "restart" in flags and key in self._restart_events:
                    self._check_restart(key, self._restart_events[key])
                if "incoming" in flags and key in self._incoming_events:
                    self._incoming_events[key].set()

            # Brief pause after a non-empty response so a flag that stays
            # pending server-side (e.g. a restart whose ack keeps failing)
            # can't turn this loop into a hot poll.
            if events and self.stop_event.wait(1):
                break
        logger.debug("[events_listener] stopping")

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
