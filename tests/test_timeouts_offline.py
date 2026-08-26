"""Offline unit test for the new timeout semantics in session.events()."""
import queue, time, threading, sys
sys.path.insert(0, "cursor2api")
from cursor2api import session as S
from cursor2api.pb import frame, msg

class FakeConn:
    def __init__(self, script=()):
        self.events = queue.Queue()
        self.closed = False
        self.events.put(("headers", {":status": "200"}))
        for payload in script:
            self.events.put(("data", frame(payload)))
    def start(self, *a): pass
    def send(self, *a, **k): pass
    def close(self): self.closed = True

def new_session(*script):
    s = S.Session(model="claude-fable-5")
    s.conn = FakeConn(script)
    return s

HB  = msg(f1=msg(f13=1))                      # pure heartbeat interaction_update
CTX = msg(f2=msg(f10=msg(f1=1)))              # exec request_context_args (control)
TXT = msg(f1=msg(f1=msg(f1=b"hi")))           # text delta
END = msg(f1=msg(f14=b""))                    # turn_ended via usage frame

# 1) stream never answered: error after first_timeout --------------------------
t0 = time.time()
s = new_session()
evs = list(s.events(first_timeout=0.7, hard_timeout=60))
assert evs[-1] == ("error", "upstream did not respond"), evs[-1]
assert time.time() - t0 < 5
print("1. never-answered: error after first_timeout  OK")

# 2) control chatter only -> first_output_timeout, explicit error + H2 close ---
def drip(conn, payload, period=0.1, count=60):
    for _ in range(count):
        if conn.closed:
            return
        conn.events.put(("data", frame(payload)))
        time.sleep(period)
t0 = time.time()
s = new_session(CTX)
probe_conn = s.conn
threading.Thread(target=drip, args=(probe_conn, HB), daemon=True).start()
evs = list(s.events(first_timeout=60, first_output_timeout=1.0, hard_timeout=30))
assert any(k == "error" and "no output" in v for k, v in evs), evs[-3:]
assert s.conn is None and probe_conn.closed, "H2 connection must be torn down on first_output_timeout"
print("2. control-only stream: explicit no-output error + H2 close  OK (%.1fs)" % (time.time() - t0))

# 3) hard timeout is now an explicit error + H2 close, not ('end','timeout') ---
t0 = time.time()
s = new_session()
probe_conn = s.conn
threading.Thread(target=drip, args=(probe_conn, HB, 0.3, 60), daemon=True).start()
evs = list(s.events(first_timeout=60, idle_stop=60, first_output_timeout=None,
                    hard_timeout=1.2))
assert evs[-1][0] == "error" and "hard timeout" in evs[-1][1], evs[-1]
assert s.conn is None and probe_conn.closed
print("3. hard timeout: explicit error + H2 close  OK (%.1fs)" % (time.time() - t0))

# 4) text + turn_ended flow intact; send_tool_results re-arms first_output -----
s = new_session(CTX, TXT, END)
evs = list(s.events(first_timeout=60, first_output_timeout=60, hard_timeout=30))
kinds = [k for k, _ in evs]
assert "text" in kinds and "end" in kinds, evs
s.send_tool_results([(0, None, "ok", False)])
assert s.first_output is False
print("4. normal text/turn_ended flow + tool-results rearm  OK", kinds)

# 5) post-tool-results control flood is cut at first_output_timeout --------------
class FC:
    def __init__(self):
        self.events = queue.Queue(); self.closed = False
        self.events.put(("headers", {":status": "200"}))
    def start(self, *a): pass
    def send(self, *a, **k): pass
    def close(self): self.closed = True

s = S.Session(model="claude-fable-5")
conn = FC(); s.conn = conn
s.send_tool_results([(0, None, "ok", False)])   # caller answered a tool use
assert s.first_output is False and s.last_activity is not None

def drip3():
    for _ in range(20):
        if conn.closed: return
        conn.events.put(("data", frame(CTX)))
        time.sleep(0.05)
threading.Thread(target=drip3, daemon=True).start()

t0 = time.time()
evs = list(s.events(first_timeout=60, first_output_timeout=0.4, hard_timeout=30))
assert any(k == "error" and "no output" in v for k, v in evs), evs[-5:]
assert conn.closed and s.conn is None
print("5. post-tool-results control flood is cut at first_output_timeout  OK")
print("ALL OFFLINE TESTS PASSED")
