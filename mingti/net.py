"""模型请求走哪条路：跟着系统的代理设置走，代理用不了就直连，开不开代理都能用。

- 每次请求都重新看一遍系统的代理设置（环境变量 HTTP_PROXY / HTTPS_PROXY；没有环境变量时，Windows 读"Internet 选项"
  里的代理，macOS 读系统设置）。以前是程序启动时读一次：开着代理启动、之后关掉代理，请求还往已经关掉的代理发，
  就一直连不上模型，要重开程序才好；反过来，没开代理时启动、之后才开代理，境外的服务也一直连不上。
- 设了代理的，先走代理；代理这条路没走通（代理软件关了但设置没清掉、代理连不上这家服务），再直连试一次。
  国内的模型服务直连就能用，境外的服务没有代理时直连自然也不通，这时把错误报出去。
- 请求发出去、等回复的时候断了，而断的原因是代理没了（刚把代理软件关掉）：换成直连把这次请求重发一遍。
- 刚失败过的路记一会儿，下次请求先试别的路，不用每次都先在不通的路上等；但哪条路都不会被跳过。
- 本机和局域网里的地址（自己电脑上、内网里的模型服务）从不走代理。

deepseek.py（兼容 OpenAI 的接口，urllib）和 claude.py（Anthropic SDK）都按这里的 Routes 选路。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator

log = logging.getLogger("mingti.net")

CONNECT_TIMEOUT = 15.0  # 连上服务（含过代理、TLS 握手）最多等这么久；连上以后等回复，用请求自己的时限
PROXY_RETRY_AFTER = 120.0  # 代理这条路没走通：这么久之内先试直连，过后再试它
DIRECT_RETRY_AFTER = 30.0  # 直连没走通：这么久之内先试代理

_lock = threading.Lock()
_down: dict[tuple[str | None, str], float] = {}  # (代理地址，None 是直连；主机名) → 这个时间之前先不走这条路
_local = threading.local()  # .route：这个线程里上一次请求走通的 (主机名, 代理地址)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _is_local(host: str) -> bool:
    """本机、局域网里的地址：不走代理。"""
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def system_proxy(url: str) -> str | None:
    """这个地址现在该走哪个代理（http://主机:端口）；None 是直连。每次调用都重新读系统的设置。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or _is_local(host):
        return None
    try:
        proxies = urllib.request.getproxies()
        if not proxies or urllib.request.proxy_bypass(host):  # 系统设置里写了这个地址不走代理
            return None
    except Exception:  # noqa: BLE001 - 读不出设置就当没有代理
        return None
    proxy = proxies.get(parts.scheme) or proxies.get("all") or ""
    if proxy and "://" not in proxy:
        proxy = f"http://{proxy}"
    # 只会用 HTTP 代理（Clash、v2rayN 这些软件设的系统代理都是）；SOCKS 的不会用，当没有
    return proxy if urllib.parse.urlsplit(proxy).scheme in ("http", "https") else None


def _label(proxy: str) -> str:
    """写日志时用的代理地址：不带用户名和密码。"""
    parts = urllib.parse.urlsplit(proxy)
    return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def reachable(proxy: str, timeout: float = 1.5) -> bool:
    """代理的端口还连得上吗（代理软件还开着吗）。"""
    parts = urllib.parse.urlsplit(proxy)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection((parts.hostname or "", port), timeout=timeout).close()
    except OSError:
        return False
    return True


def proxy_gone(url: str, proxy: str | None) -> bool:
    """刚才走的这个代理现在是不是没了：系统设置里已经不是它（代理关了），或者它的端口连不上了。"""
    if not proxy:
        return False
    return system_proxy(url) != proxy or not reachable(proxy)


def _mark_down(route: str | None, host: str) -> bool:
    """记下这条路刚失败过；返回是不是新记的（之前没记着）。"""
    now = time.monotonic()
    with _lock:
        fresh = _down.get((route, host), 0) <= now
        _down[(route, host)] = now + (PROXY_RETRY_AFTER if route else DIRECT_RETRY_AFTER)
    return fresh


class Routes:
    """一次请求可以走的路，按先后排好；每一条是代理地址，None 表示直连。

        plan = Routes(url)
        for route in plan:
            try:
                ...按 route 发请求...
            except 没连上:
                if not plan.failed(route, 原因):
                    raise  # 没有别的路了
            else:
                plan.succeeded(route)
                break
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.host = _host(url)
        proxy = system_proxy(url)
        candidates: list[str | None] = [proxy, None] if proxy else [None]
        now = time.monotonic()
        with _lock:
            good = [route for route in candidates if _down.get((route, self.host), 0) <= now]
        # 没问题的排前面，刚失败过的垫后。哪条都不跳过：说不定这会儿又通了（网络刚恢复、代理刚开）
        self.order = good + [route for route in candidates if route not in good]
        self._index = 0
        _local.route = None

    def __iter__(self) -> Iterator[str | None]:
        for index, route in enumerate(self.order):
            self._index = index
            yield route

    def failed(self, route: str | None, reason: object = "", *, sent: bool = False) -> bool:
        """这条路没走通。返回还有没有下一条路可以试；没有的话，调用的地方该把错误报出去。

        sent=True：请求已经发出去，是等回复时断的。这时只有断的原因是代理没了（比如刚关掉代理软件）才换路重发，
        否则多半是服务那边的问题，重发一遍也没用，照原样报错。"""
        if sent and not proxy_gone(self.url, route):
            return False
        more = self._index + 1 < len(self.order)
        if _mark_down(route, self.host) and more:
            if route:
                log.warning("代理 %s 走不通（%s），改为直连 %s", _label(route), reason, self.host)
            else:
                log.warning("直连 %s 不通（%s），改走代理 %s", self.host, reason, _label(self.order[self._index + 1] or ""))
        return more

    def succeeded(self, route: str | None) -> None:
        with _lock:
            _down.pop((route, self.host), None)
        _local.route = (self.host, route)


def last_route_gone(url: str) -> bool:
    """这个线程里刚才那次请求是走代理连上的，现在那个代理没了。
    给 claude.py 用：回复读到一半断了，是这个原因的话就值得重发一次（重发时会改走直连）。"""
    host, proxy = getattr(_local, "route", None) or ("", None)
    if host != _host(url) or not proxy_gone(url, proxy):
        return False
    if _mark_down(proxy, host):
        log.warning("代理 %s 没了（回复读到一半断开），改为直连 %s 重发", _label(proxy or ""), host)
    return True
