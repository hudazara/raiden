import atexit
import signal
from abc import ABC, abstractmethod
from types import TracebackType
from typing import Any, Callable, List, Optional, Set, Type

import gevent
import structlog
from gevent import Greenlet
from gevent.event import AsyncResult
from gevent.greenlet import SpawnedLink
from gevent.lock import RLock
from gevent.subprocess import Popen, TimeoutExpired

log = structlog.get_logger(__name__)
STATUS_CODE_FOR_SUCCESS = 0


class Nursery(ABC):
    @abstractmethod
    def exec_under_watch(self, process_args: List[str], **kwargs: Any) -> Optional[Popen]:
        pass

    @abstractmethod
    def spawn_under_watch(self, function: Callable, *args: Any, **kargs: Any) -> Greenlet:
        pass

    @abstractmethod
    def wait(self, timeout: Optional[float]) -> None:
        pass


class Janitor:
    """Tries to properly stop all subprocesses before quitting the script.

    - This watches for the status of the subprocess, if the processes exits
      with a non-zero error code then the failure is propagated.
    - If for any reason this process is dying, then all the spawned processes
      have to be killed in order for a proper clean up to happen.
    """

    def __init__(self, stop_timeout: float = 20) -> None:
        self.stop_timeout = stop_timeout
        self._stop = AsyncResult()
        self._processes: Set[Popen] = set()

        # Lock to protect changes to `_stop` and `_processes`. The `_stop`
        # synchronization is necessary to fix the race described below,
        # `_processes` synchronization is necessary to avoid iteration over a
        # changing container.
        #
        # Important: It is very important to register any executed subprocess,
        # otherwise no signal will be sent during shutdown and the subprocess
        # will become orphan. To properly register the subprocesses it is very
        # important to finish any pending call to `exec_under_watch` before
        # exiting the `Janitor`, and if the exit does run, `exec_under_watch`
        # must not start a new process.
        #
        # Note this only works if the greenlet that instantiated the Janitor
        # itself has a chance to run.
        self._processes_lock = RLock()

    def __enter__(self) -> Nursery:
        # Registers an atexit callback in case the __exit__ doesn't get a
        # chance to run. This happens when the Janitor is not used in the main
        # greenlet, and its greenlet is not the one that is dying.
        atexit.register(self._free_resources)

        # Hide the nursery to require the context manager to be used. This
        # leads to better behavior in the happy case since the exit handler is
        # used.
        janitor = self

        class ProcessNursery(Nursery):
            @staticmethod
            def exec_under_watch(process_args: List[str], **kwargs: Any) -> Optional[Popen]:
                assert not kwargs.get("shell", False), "Janitor does not work with shell=True"

                def subprocess_stopped(result: AsyncResult) -> None:
                    with janitor._processes_lock:
                        # Processes are expected to quit while the nursery is
                        # active, remove them from the track list to clear memory
                        janitor._processes.remove(process)

                        # if the subprocess error'ed propagate the error.
                        try:
                            exit_code = result.get()
                            if exit_code != STATUS_CODE_FOR_SUCCESS:
                                log.error(
                                    "Process died! Bailing out.",
                                    args=process.args,
                                    exit_code=exit_code,
                                )
                                exception = SystemExit(exit_code)
                                janitor._stop.set_exception(exception)
                        except Exception as exception:
                            janitor._stop.set_exception(exception)

                with janitor._processes_lock:
                    if janitor._stop.ready():
                        return None

                    process = Popen(process_args, **kwargs)
                    janitor._processes.add(process)

                    # `rawlink`s are executed from within the hub, the problem
                    # is that locks can not be acquire at that point.
                    # SpawnedLink creates a new greenlet to run the callback to
                    # circumvent that.
                    callback = SpawnedLink(subprocess_stopped)

                    process.result.rawlink(callback)

                    # Important: `stop` may be set after Popen started, but before
                    # it returned. If that happens `GreenletExit` exception is
                    # raised here. In order to have proper cleared, exceptions have
                    # to be handled and the process installed.
                    if janitor._stop.ready():
                        process.send_signal(signal.SIGINT)

                    return process

            @staticmethod
            def spawn_under_watch(function: Callable, *args: Any, **kwargs: Any) -> Greenlet:
                greenlet = gevent.spawn(function, *args, **kwargs)

                # The Event.rawlink is executed inside the Hub thread, which
                # does validation and *raises on blocking calls*, to go around
                # this a new greenlet has to be spawned, that in turn will
                # raise the exception.
                def spawn_to_kill() -> None:
                    gevent.spawn(greenlet.throw, gevent.GreenletExit())

                janitor._stop.rawlink(lambda _stop: spawn_to_kill())
                return greenlet

            @staticmethod
            def wait(timeout: Optional[float]) -> None:
                janitor._stop.wait(timeout)

        return ProcessNursery()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        with self._processes_lock:
            # Make sure to signal that we are exiting.
            if not self._stop.done():
                self._stop.set()

            self._free_resources()

            # Behave nicely if context manager's __exit__ is executed. This
            # implements the expected behavior of a context manager, which will
            # clear the resources when exiting.
            atexit.unregister(self._free_resources)

        self._stop.get()
        return None

    def _free_resources(self) -> None:
        with self._processes_lock:
            for p in self._processes:
                p.send_signal(signal.SIGINT)

            try:
                for p in self._processes:
                    exit_code = p.wait(timeout=self.stop_timeout)
                    if exit_code != STATUS_CODE_FOR_SUCCESS:
                        log.warning(
                            "Process did not exit cleanly",
                            exit_code=exit_code,
                            communicate=p.communicate(),
                        )
            except TimeoutExpired:
                log.warning(
                    "Process did not stop in time. Sending SIGKILL to all remaining processes!",
                    command=p.args,
                )
                for p in self._processes:
                    p.send_signal(signal.SIGKILL)
