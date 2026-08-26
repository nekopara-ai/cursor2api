"""Minimal true-bidirectional HTTP/2 client (h2 over TLS) for Connect streaming.

Connections are pre-warmed: TLS + ALPN + the HTTP/2 preface cost a couple of
round trips to Cursor's global endpoint, which would otherwise be paid on every
request before the first token can even be asked for. `acquire()` hands out a
ready connection and refills the pool in the background.
"""
import os, select, socket, ssl, threading, queue, time
import h2.connection, h2.events, h2.config

POOL_SIZE = int(os.environ.get("CURSOR2API_POOL", "2"))
MAX_IDLE = float(os.environ.get("CURSOR2API_POOL_IDLE", "40"))

_pool = []
_pool_lock = threading.Lock()
_filling = set()



def _open_socket(host, port):
    import base64
    from urllib.parse import urlsplit
    proxy = (os.environ.get("CURSOR2API_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTPS_PROXY"))
    if not proxy:
        return socket.create_connection((host, port), timeout=30)
    u = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    raw = socket.create_connection((u.hostname, u.port or 8080), timeout=30)
    auth = ""
    if u.username:
        cred = base64.b64encode(f"{u.username}:{u.password or ''}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {cred}\r\n"
    raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = raw.recv(4096)
        if not chunk:
            break
        resp += chunk
    code = int(resp.split(b" ", 2)[1])
    assert 200 <= code < 300, f"CONNECT {host}:{port} via proxy failed: {resp[:120]!r}"
    return raw

class BidiH2:
    def __init__(self, host, port=443):
        self.host=host
        ctx=ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])
        # Optional HTTP CONNECT when CURSOR2API_PROXY / https_proxy is set.
        raw=_open_socket(host, port)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock=ctx.wrap_socket(raw, server_hostname=host)
        assert self.sock.selected_alpn_protocol()=="h2", "no h2 ALPN"
        self.conn=h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
        self.conn.initiate_connection()
        self._flush()
        self.lock=threading.Lock()
        self.events=queue.Queue()
        self.resp_headers=None
        self.stream_id=None
        self.closed=False
        self.created=time.time()
        self.window=threading.Condition()  # signalled on WINDOW_UPDATE
        self.drain()                       # settings/window frames sent on connect

    def _flush(self):
        data=self.conn.data_to_send()
        if data: self.sock.sendall(data)

    def drain(self, wait=0.0):
        """Feeds whatever the peer already sent (settings, GOAWAY) to h2.

        Returns False when the connection is no longer usable, so a pooled
        connection can be dropped instead of being handed out dead.
        """
        try:
            while select.select([self.sock], [], [], wait)[0] or self.sock.pending():
                wait = 0.0
                data = self.sock.recv(65536)
                if not data:
                    return False
                with self.lock:
                    for e in self.conn.receive_data(data):
                        if isinstance(e, (h2.events.ConnectionTerminated,
                                          h2.events.StreamReset)):
                            return False
                    self._flush()
        except Exception:
            return False
        return not self.closed

    def start(self, path, headers):
        self.stream_id=self.conn.get_next_available_stream_id()
        hdrs=[(":method","POST"),(":authority",self.host),(":scheme","https"),(":path",path)]
        hdrs+=[(k.lower(),v) for k,v in headers.items()]
        with self.lock:
            self.conn.send_headers(self.stream_id, hdrs, end_stream=False)
            self._flush()
        self.reader=threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def send(self, payload, end=False):
        """Writes a payload, blocking on WINDOW_UPDATE when the peer's window is full.

        Long prompts exceed the initial 64KB stream window, so chunks must be sized by
        the live flow-control window, not just max_frame_size.
        """
        i=0
        while i < len(payload):
            with self.lock:
                room=min(self.conn.local_flow_control_window(self.stream_id),
                         self.conn.max_outbound_frame_size, len(payload)-i)
                if room > 0:
                    self.conn.send_data(self.stream_id, payload[i:i+room], end_stream=False)
                    self._flush()
                    i+=room
            if room <= 0:
                with self.window:
                    self.window.wait(10)
                if self.closed:
                    raise OSError("connection closed while sending")
        with self.lock:
            if end: self.conn.end_stream(self.stream_id)
            self._flush()

    def _read_loop(self):
        try:
            while not self.closed:
                data=self.sock.recv(65536)
                if not data:
                    self.events.put(("closed",None)); return
                with self.lock:
                    evs=self.conn.receive_data(data)
                    self._flush()
                for e in evs:
                    if isinstance(e,h2.events.ResponseReceived):
                        self.events.put(("headers",dict((k.decode(),v.decode()) for k,v in e.headers)))
                    elif isinstance(e,h2.events.DataReceived):
                        with self.lock:
                            self.conn.acknowledge_received_data(e.flow_controlled_length,e.stream_id)
                            self._flush()
                        self.events.put(("data",e.data))
                    elif isinstance(e,h2.events.WindowUpdated):
                        with self.window: self.window.notify_all()
                    elif isinstance(e,(h2.events.StreamEnded,h2.events.StreamReset,
                                       h2.events.ConnectionTerminated)):
                        self.events.put(("end",type(e).__name__))
                        self.closed=True
                        with self.window: self.window.notify_all()
                        return
        except Exception as ex:
            self.events.put(("error",repr(ex)))
            self.closed=True
            with self.window: self.window.notify_all()

    def close(self):
        """Tears the connection down without touching the SSL object concurrently.

        SSLSocket.close() would run SSL_shutdown while the reader thread sits in
        SSL_read on the same object, which segfaults libcrypto under load; a plain
        socket-level shutdown wakes the reader and lets it unwind first.
        """
        self.closed=True
        with self.window: self.window.notify_all()
        try: socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        except Exception: pass
        t=getattr(self,"reader",None)
        if t is not None and t is not threading.current_thread():
            t.join(5)
        with self.lock:
            try: socket.socket.close(self.sock)
            except Exception: pass


def acquire(host):
    """A connected, unused BidiH2 for `host`, from the pool when possible."""
    while True:
        with _pool_lock:
            hit = next((i for i, c in enumerate(_pool) if c.host == host), None)
            conn = _pool.pop(hit) if hit is not None else None
        prewarm(host)
        if conn is None:
            return BidiH2(host)
        if time.time() - conn.created < MAX_IDLE and conn.drain():
            return conn
        try:
            conn.close()
        except Exception:
            pass


def prewarm(host, count=None):
    """Tops the pool up in the background; safe to call on every request."""
    want = POOL_SIZE if count is None else count
    if want <= 0:
        return
    with _pool_lock:
        if host in _filling or sum(1 for c in _pool if c.host == host) >= want:
            return
        _filling.add(host)

    def fill():
        try:
            while True:
                with _pool_lock:
                    if sum(1 for c in _pool if c.host == host) >= want:
                        return
                conn = BidiH2(host)
                with _pool_lock:
                    _pool.append(conn)
        except Exception:
            pass
        finally:
            with _pool_lock:
                _filling.discard(host)

    threading.Thread(target=fill, daemon=True).start()
