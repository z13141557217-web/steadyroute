"""Admission barrier shared by every worker I/O and settings transaction.

Close admission before waiting. Nested calls belonging to an admitted transaction
may finish (including rollback); late futures cannot admit a new transaction.
The controller only reports stopped after this barrier AND process exit.
"""
import contextlib
import functools
import threading
import time


class Stopped(RuntimeError):
    pass


class Gate:
    def __init__(self):
        self.condition = threading.Condition()
        self.local = threading.local()
        self.accepting = True
        self.active = 0

    @contextlib.contextmanager
    def operation(self):
        depth = getattr(self.local, 'depth', 0)
        with self.condition:
            if not depth:
                if not self.accepting:
                    raise Stopped('稳航正在停止或已暂停；本次操作未提交')
                self.active += 1
            self.local.depth = depth + 1
        try:
            yield
        finally:
            with self.condition:
                self.local.depth -= 1
                if not depth:
                    self.active -= 1
                    self.condition.notify_all()

    def close(self, timeout=180):
        deadline = time.monotonic() + timeout
        with self.condition:
            self.accepting = False
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('在途操作尚未结束；保持禁止新操作，未报告停止成功')
                self.condition.wait(remaining)


gate = Gate()


def guarded(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with gate.operation():
            return function(*args, **kwargs)
    return wrapped
