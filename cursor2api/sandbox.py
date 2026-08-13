"""Client-side executor for the agent's builtin tools, confined to a scratch dir.

Cursor's agent protocol expects the *client* to run file/shell tools, and the
stream stalls if an ExecServerMessage is never answered. Attached documents in
particular are pushed down as a `write_args` (Cursor asks us to materialize the
upload) and then read back, so a chat proxy still needs working write/read.

Every path is rewritten into ROOT, so the model cannot touch the real filesystem.
Shell is denied unless SANDBOX_SHELL=1.

Handled ExecServerMessage fields:
  2 shell_args   3 write_args   4 delete_args   5 grep_args
  7 read_args    8 ls_args     14 shell_stream_args
"""
import os, re, shutil, subprocess, tempfile
from .pb import msg, get, getvar

ROOT = os.environ.get("SANDBOX_ROOT") or os.path.join(tempfile.gettempdir(), "cursor-sandbox")
SHELL_OK = os.environ.get("SANDBOX_SHELL") == "1"
MAX_READ = 400_000


def _local(path):
    """Map any model-supplied path into the sandbox."""
    p = (path or "").strip() or "/file"
    p = re.sub(r"^[A-Za-z]:", "", p)
    p = os.path.normpath("/" + p.lstrip("/"))
    full = os.path.normpath(os.path.join(ROOT, p.lstrip("/")))
    if not full.startswith(os.path.realpath(ROOT)) and not full.startswith(ROOT):
        full = os.path.join(ROOT, os.path.basename(p))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def _read_text(fp):
    with open(fp, "rb") as f:
        raw = f.read(MAX_READ)
    return raw.decode("utf-8", "replace")


def handle(field, a, redirect=None):
    """Returns [(ExecClientMessage field, encoded result), ...] or None.

    shell_stream is answered with several messages (start/stdout/exit) rather than
    a single result, hence the list.

    redirect: when the API caller declared its own tools, the sandbox is useless to
    it, so the builtin tool is refused with this text and the model retries with the
    caller's tool instead.
    """
    if redirect:
        return [refuse(field, redirect)]
    out = _handle(field, a)
    if out is None:
        return None
    return out if isinstance(out, list) else [out]


# ExecServerMessage field -> (result field, error message with the text at f2,
# except GrepError which carries it at f1 and the shell errors at f3).
ERROR_FIELD = {
    2:  (2,  lambda t: msg(f7=msg(f3=t))),      # ShellResult.permission_denied
    3:  (3,  lambda t: msg(f5=msg(f2=t))),      # WriteResult.error
    4:  (4,  lambda t: msg(f7=msg(f2=t))),      # DeleteResult.error
    5:  (5,  lambda t: msg(f2=msg(f1=t))),      # GrepResult.error
    7:  (7,  lambda t: msg(f2=msg(f2=t))),      # ReadResult.error
    8:  (8,  lambda t: msg(f2=msg(f2=t))),      # LsResult.error
    14: (14, lambda t: msg(f6=msg(f3=t))),      # ShellStream.permission_denied
    29: (29, lambda t: msg(f2=msg(f2=t))),      # redacted read
    52: (55, lambda t: msg(f7=msg(f3=t))),
}


def refuse(field, text):
    rf, enc = ERROR_FIELD.get(field, (field, lambda t: msg(f2=msg(f2=t))))
    return rf, enc(text)


def _handle(field, a):
    if field == 3:                                    # write
        path = (get(a, 1) or b"").decode()
        text = get(a, 2)
        data = get(a, 5)
        fp = _local(path)
        blob = data if data else (text or b"")
        with open(fp, "wb") as f:
            f.write(blob)
        succ = {"f1": path, "f2": blob.count(b"\n") + 1, "f3": len(blob)}
        if getvar(a, 4):
            succ["f4"] = blob.decode("utf-8", "replace")[:MAX_READ]
        return 3, msg(f1=msg(**succ))

    if field == 7 or field == 29:                     # read / redacted_read
        path = (get(a, 1) or b"").decode()
        fp = _local(path)
        if not os.path.isfile(fp):
            return field, msg(f4=msg(f1=path))        # file_not_found
        content = _read_text(fp)
        off = getvar(a, 4) or 0
        lim = getvar(a, 5) or 0
        lines = content.splitlines(True)
        sel = lines[off:off + lim] if lim else lines[off:]
        return field, msg(f1=msg(f1=path, f2="".join(sel), f3=len(lines),
                                 f4=os.path.getsize(fp), f6=bool(lim and len(lines) > off + lim),
                                 f8=bool(off or lim)))

    if field == 8:                                    # ls
        path = (get(a, 1) or b"").decode()
        fp = _local(path)
        names = sorted(os.listdir(fp)) if os.path.isdir(fp) else []
        tree = msg(f1=path, f2=[msg(f1=n) for n in names])
        return 8, msg(f1=msg(f1=tree))

    if field == 4:                                    # delete
        path = (get(a, 1) or b"").decode()
        fp = _local(path)
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        elif os.path.exists(fp):
            os.remove(fp)
        return 4, msg(f1=msg(f1=path))

    if field == 5:                                    # grep
        pattern = (get(a, 1) or b"").decode()
        path = (get(a, 2) or b"/").decode()
        fp = _local(path)
        try:
            out = subprocess.run(["grep", "-rn", "-e", pattern, fp], capture_output=True,
                                 timeout=20).stdout.decode("utf-8", "replace")[:MAX_READ]
        except Exception as e:
            return 5, msg(f2=msg(f1=str(e)))
        return 5, msg(f1=msg(f1=pattern, f2=path, f3="content",
                             f4=msg(f1=path, f2=msg(f1=[out]))))

    if field in (2, 14, 52):                          # shell / shell_stream / mini_swe bash
        cmd = (get(a, 1) or b"").decode()
        rf = {14: 14, 52: 55}.get(field, 2)
        if not SHELL_OK:
            return [refuse(field, "shell is disabled in this sandbox")]
        os.makedirs(ROOT, exist_ok=True)
        try:
            r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, timeout=60)
            code, so, se = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            code, so, se = 124, e.stdout or b"", (e.stderr or b"") + b"\ntimed out"
        except Exception as e:
            if rf == 14:
                return [(14, msg(f3=msg(f1=1, f2=ROOT)))]
            return rf, msg(f5=msg(f1=cmd, f2=ROOT, f3=str(e)))
        so = so.decode("utf-8", "replace")[:MAX_READ]
        se = se.decode("utf-8", "replace")[:20000]
        if rf == 14:                                  # ShellStream events
            evs = [(14, msg(f4=msg(f1=b"")))]
            if so:
                evs.append((14, msg(f1=msg(f1=so))))
            if se:
                evs.append((14, msg(f2=msg(f1=se))))
            evs.append((14, msg(f3=msg(f1=code, f2=ROOT))))
            return evs
        return rf, msg(f1=msg(f1=cmd, f2=ROOT, f3=code, f4="", f5=so, f6=se, f7=0))
    return None
