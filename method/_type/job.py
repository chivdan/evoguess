import threading

from util.array import unzip, none
from util.error import AlreadyRunning, CancelledError

PENDING = 0
RUNNING = 1
FINISHED = 2
CANCELLED = 3

TIMEOUT_ERROR = 'TimeoutError'
CANCELLED_ERROR = 'CancelledError'


class _FirstCompletedWaiter:
    def __init__(self):
        self.event = threading.Event()
        self.finished_jobs = []

    def add_result(self, job):
        self.finished_jobs.append(job)
        self.event.set()


class _AcquireJobs(object):
    def __init__(self, jobs):
        self.jobs = sorted(jobs, key=id)

    def __enter__(self):
        for job in self.jobs:
            job._condition.acquire()

    def __exit__(self, *args):
        for job in self.jobs:
            job._condition.release()


def _create_and_install_waiters(jobs):
    waiter = _FirstCompletedWaiter()
    for job in jobs:
        job._waiters.append(waiter)
    return waiter


def first_completed(jobs, timeout=None):
    with _AcquireJobs(jobs):
        done = [job for job in jobs if job._state > RUNNING]
        if len(done) > 0: return done
        waiter = _create_and_install_waiters(jobs)

    waiter.event.wait(timeout)
    for job in jobs:
        with job._condition:
            job._waiters.remove(waiter)

    return waiter.finished_jobs


class Job:
    def __init__(self, context):
        self._offset = 0
        self._indexes = []
        self._futures = []
        self._results = []
        self._waiters = []
        self._state = PENDING
        self.context = context

        self._condition = threading.Condition()
        self._processor = threading.Thread(
            target=self._process, args=(context,)
        )

    def _handle_future(self, future, i=None):
        with self._condition:
            i = i or self._futures.index(future)
            try:
                result = future.result(timeout=0)
                for j, index in enumerate(self._indexes[i]):
                    self._results[index] = result[j]
            except Exception as e:
                if type(e).__name__ != CANCELLED_ERROR:
                    print(f'Exception in task {i}: ', repr(e))

    def _process(self, context):
        fn = context.function.get_function()
        awaiter = context.executor.get_awaiter()

        cases = []
        tasks = context.get_tasks(cases, self._offset)
        while self.running() and len(tasks) > 0:
            index_futures = context.executor.submit_all(fn, *tasks)
            indexes, futures = unzip(index_futures)

            with self._condition:
                self._offset += len(tasks)
                self._indexes.extend(indexes)
                self._futures.extend(futures)
                self._results.extend(none(tasks))

            count, timeout = context.get_limits(cases, self._offset)
            for future in awaiter(self._futures, timeout):
                self._handle_future(future)

            cases = [result for result in self._results if result]
            tasks = context.get_tasks(cases, self._offset)

        with self._condition:
            if self._state == RUNNING:
                self._state = FINISHED

            for waiter in self._waiters:
                waiter.add_result(self)
            self._condition.notify_all()

    def start(self):
        if self._state != PENDING:
            raise AlreadyRunning()

        self._state = RUNNING
        self._processor.start()
        return self

    def cancel(self):
        with self._condition:
            if self._state in [CANCELLED, FINISHED]:
                return True

            if self._state == RUNNING:
                self._state = CANCELLED
                for future in self._futures:
                    future.cancel()
                self._condition.notify_all()

        return False

    def cancelled(self):
        with self._condition:
            return self._state == CANCELLED

    def running(self):
        with self._condition:
            return self._state == RUNNING

    def done(self):
        with self._condition:
            return self._state > RUNNING

    def result(self, timeout):
        with self._condition:
            if self._state == CANCELLED:
                raise CancelledError()
            elif self._state == FINISHED:
                return self._results

            self._condition.wait(timeout)

            if self._state == CANCELLED:
                raise CancelledError()
            elif self._state == FINISHED:
                return self._results
            else:
                raise TimeoutError()

    def join(self):
        self._processor.join()
