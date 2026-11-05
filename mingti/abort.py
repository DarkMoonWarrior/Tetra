"""马上停下：用户点了停止，正在进行的模型请求当场掐断，不等它返回。

每个出题任务（批量出题、组卷、导入也一样）有一个 AbortScope。调用模型时，JobRecorder.track() 把它设成
当前线程的"作用域"；发请求的地方（deepseek.py 的 HTTP 连接、claude.py 的流）把连接登记进来。
用户点停止时调用 scope.abort()：登记过的连接全部断开，卡在等回复的请求立刻报错（转成 StopRequested），
之后这个任务也不会再发新的请求。

连接一断，模型服务就不再往下生成；已经生成的部分可能照常计费。
"""

from __future__ import annotations

import _socket
import socket
import threading
from contextlib import contextmanager
from typing import Callable, Iterator


class StopRequested(Exception):
    """别的命题组已经交卷，或用户点了停止：不再调用模型。"""


_local = threading.local()


def cut(sock: socket.socket) -> None:
    """断开连接：正卡在读这条连接的线程会马上收到错误。

    先 shutdown（Linux、macOS 上这样才能叫醒正在读的线程），再直接关掉底层连接（Windows 上 shutdown 叫不醒，
    要关掉才行；实测两种情况都在 0.01 秒内报错）。两步都调 _socket 底层的方法：SSLSocket.shutdown 会清掉
    另一个线程正在用的 SSL 对象；socket.close 在 http.client 还拿着连接读回复时只记个标记、并不真关。"""
    try:
        if sock.fileno() == -1:
            return
    except OSError:
        return
    try:
        _socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        _socket.socket.close(sock)
    except OSError:
        pass


class AbortScope:
    """一个任务里所有正在进行的模型请求；abort() 一次全部掐断。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._closers: dict[int, Callable[[], None]] = {}
        self._next = 0
        self.aborted = False

    def check(self) -> None:
        if self.aborted:
            raise StopRequested()

    def watch_socket(self, sock: socket.socket) -> None:
        """登记一条连接；已经停了就当场断开并报 StopRequested。"""
        with self._lock:
            if not self.aborted:
                # 用完关掉的连接顺手清掉，免得越积越多
                self._sockets = [s for s in self._sockets if s.fileno() != -1]
                self._sockets.append(sock)
                return
        cut(sock)
        raise StopRequested()

    def watch(self, closer: Callable[[], None]) -> int:
        """登记一个掐断的办法（比如关掉 Claude 的流），返回编号；请求结束后 unwatch。"""
        with self._lock:
            if self.aborted:
                raise StopRequested()
            self._next += 1
            self._closers[self._next] = closer
            return self._next

    def unwatch(self, token: int) -> None:
        with self._lock:
            self._closers.pop(token, None)

    def abort(self) -> None:
        with self._lock:
            if self.aborted:
                return
            self.aborted = True
            sockets, self._sockets = self._sockets, []
            closers = list(self._closers.values())
            self._closers.clear()
        for sock in sockets:
            cut(sock)
        for closer in closers:
            try:
                closer()
            except Exception:  # noqa: BLE001 - 掐断时出什么错都不要紧，等回复的那边会自己报错
                pass


@contextmanager
def active(scope: AbortScope | None) -> Iterator[None]:
    """这段代码里调用模型时，请求登记到 scope；None 表示沿用外面的（外面也没有就照常等回复）。"""
    if scope is None:
        yield
        return
    previous = getattr(_local, "scope", None)
    _local.scope = scope
    try:
        yield
    finally:
        _local.scope = previous


def current() -> AbortScope | None:
    return getattr(_local, "scope", None)


def aborted() -> bool:
    scope = current()
    return bool(scope and scope.aborted)
