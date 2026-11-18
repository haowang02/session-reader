#!/usr/bin/env python3
"""Read local session history of coding agents: Claude Code, Codex, OpenCode, Antigravity (agy).

  markdown (default)  visible User / Assistant dialogue only, in chronological order
  json                full session: metadata, a normalized event timeline (messages,
                      thinking, tool calls, tool results, other events) and the
                      original records

Everything is read-only and uses only the Python standard library (3.9+).
Run with --help for usage.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import collections
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

AGENTS = ("claude", "codex", "opencode", "agy")
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "agy": "Antigravity (agy)"}
AGENT_ALIASES = {
    "claude": "claude", "claude-code": "claude", "claudecode": "claude", "cc": "claude",
    "codex": "codex", "openai-codex": "codex",
    "opencode": "opencode", "open-code": "opencode", "oc": "opencode",
    "agy": "agy", "antigravity": "agy", "antigravity-cli": "agy", "ag": "agy",
}
# Environment variables that hold the id of the session this script is running inside.
CURRENT_SESSION_ENV = {
    "claude": ("CLAUDE_CODE_SESSION_ID",),
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
    "opencode": ("OPENCODE_SESSION_ID",),
    "agy": (),
}


class ReaderError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


class ZstdUnavailable(ReaderError):
    pass


@dataclass
class SessionInfo:
    agent: str
    id: str
    path: str
    title: str = ""
    projects: list = field(default_factory=list)
    created: float | None = None
    updated: float | None = None
    parent_id: str | None = None
    is_child: bool = False
    empty: bool = False
    locator: dict = field(default_factory=dict)

    @property
    def project(self) -> str:
        return self.projects[0] if self.projects else ""

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "id": self.id, "title": self.title, "project": self.project,
            "projects": self.projects, "created_at": iso(self.created), "updated_at": iso(self.updated),
            "parent_id": self.parent_id, "is_subagent": self.is_child, "empty": self.empty, "path": self.path,
        }


@dataclass
class Session:
    info: SessionInfo
    storage: str
    meta: dict
    events: list
    raw: list
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers

def home() -> Path:
    return Path(os.path.expanduser("~"))


def warn(msg: str) -> None:
    print(f"session-reader: {msg}", file=sys.stderr)


def to_epoch(v) -> float | None:
    """Epoch seconds from epoch s/ms/us numbers or ISO-8601 strings (naive = UTC).

    Values outside 1973..2286 (e.g. Go's zero time 0001-01-01) are treated as missing.
    """
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e14:
            v /= 1e6
        elif v > 1e11:
            v /= 1e3
        return v if 1e8 < v < 1e10 else None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return to_epoch(float(s))
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        try:
            return to_epoch(d.timestamp())
        except (OverflowError, ValueError, OSError):
            return None
    return None


def iso(ts: float | None) -> str | None:
    try:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, OverflowError, ValueError, OSError):
        return None


def local_time(ts: float | None, tz: bool = False) -> str:
    try:
        d = dt.datetime.fromtimestamp(ts).astimezone()
    except (TypeError, OverflowError, ValueError, OSError):
        return ""
    return d.strftime("%Y-%m-%d %H:%M:%S %z" if tz else "%Y-%m-%d %H:%M:%S")


def short(text, n: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def maybe_json(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            v = v.decode("utf-8")
        except UnicodeDecodeError:
            return {"bytes_base64": base64.b64encode(v).decode("ascii")}
    if isinstance(v, str) and v[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def file_uri_to_path(u):
    if isinstance(u, str) and u.startswith("file://"):
        return unquote(urlparse(u).path)
    return u


def path_forms(p: str) -> set:
    s = os.path.expanduser(str(p))
    forms = {os.path.normpath(os.path.abspath(s))}
    try:
        forms.add(os.path.realpath(s))
    except OSError:
        pass
    return forms


def project_match(projects, target: str) -> bool:
    targets = path_forms(target)
    for p in projects:
        if not p:
            continue
        for pf in path_forms(p):
            for t in targets:
                if pf == t or pf.startswith(t.rstrip(os.sep) + os.sep):
                    return True
    return False


def pretty_path(p: str) -> str:
    h = str(home())
    return "~" + p[len(h):] if p and (p == h or p.startswith(h + os.sep)) else (p or "")


def ev(kind: str, ts=None, idx=None, **fields) -> dict:
    """Build a normalized event; None-valued fields are dropped."""
    e = {"type": kind}
    t = to_epoch(ts)
    if t is not None:
        e["timestamp"] = iso(t)
    e.update({k: v for k, v in fields.items() if v is not None})
    if idx is not None:
        e["raw_index"] = idx
    return e


def parse_jsonish(s):
    if isinstance(s, str) and s.strip()[:1] in ("{", "["):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return s


def iter_json_lines(text: str):
    # split on "\n" only: str.splitlines() would also split on U+2028 inside JSON strings
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            yield {"_unparsed": line}


def read_jsonl(path) -> list:
    return list(iter_json_lines(read_bytes(path).decode("utf-8", "replace")))


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def read_bytes(path) -> bytes:
    data = Path(path).read_bytes()
    if data[:4] == ZSTD_MAGIC:
        return zstd_decompress(data, path)
    return data


def read_head(path, n: int = 256 * 1024) -> bytes:
    """First n decompressed bytes, without decompressing a whole .zst file when avoidable.

    256 KiB holds a Codex session_meta line, which embeds the full base instructions (~20-60 KiB).
    """
    with open(path, "rb") as f:
        data = f.read(n)
    if data[:4] != ZSTD_MAGIC:
        return data
    try:
        from compression import zstd
        with zstd.open(path, "rb") as f:
            return f.read(n)
    except ImportError:
        pass
    exe = shutil.which("zstd") or shutil.which("unzstd")
    if exe:
        proc = subprocess.Popen([exe, "-dcq", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            return proc.stdout.read(n)
        finally:
            proc.kill()
            proc.wait()
    return read_bytes(path)[:n]


def zstd_decompress(data: bytes, path) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard  # optional third-party module, used only if already installed
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except ImportError:
        pass
    exe = shutil.which("zstd") or shutil.which("unzstd")
    if exe:
        r = subprocess.run([exe, "-dcq", "--", str(path)], capture_output=True)
        if r.returncode == 0:
            return r.stdout
        raise ReaderError(f"zstd failed on {path}: {r.stderr.decode(errors='replace').strip()}")
    raise ZstdUnavailable(f"{path} is zstd-compressed; use Python 3.14+ or install the zstd CLI")


_TEMP_DIRS: list = []
atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _TEMP_DIRS])


def open_sqlite(path) -> sqlite3.Connection:
    """Open a SQLite database without ever writing to it.

    mode=ro first; if that fails (e.g. WAL db whose -shm cannot be created) fall back to
    immutable=1 when there is no pending WAL, otherwise query a private temp copy.
    """
    p = Path(path).resolve()
    uri = p.as_uri()
    for suffix in ("?mode=ro", None):
        if suffix is None:
            wal = Path(str(p) + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                break
            suffix = "?mode=ro&immutable=1"
        try:
            con = sqlite3.connect(uri + suffix, uri=True)
            con.execute("select count(*) from sqlite_master").fetchone()
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.DatabaseError:
            continue
    tmp = tempfile.mkdtemp(prefix="session-reader-")
    _TEMP_DIRS.append(tmp)
    dst = Path(tmp) / p.name
    shutil.copyfile(p, dst)
    shutil.copyfile(str(p) + "-wal", str(dst) + "-wal")
    con = sqlite3.connect(str(dst))
    con.row_factory = sqlite3.Row
    return con


def sqlite_tables(con) -> set:
    return {r[0] for r in con.execute("select name from sqlite_master where type in ('table','view')")}


def mtime(*paths) -> float | None:
    times = []
    for p in paths:
        try:
            times.append(os.stat(p).st_mtime)
        except OSError:
            pass
    return max(times) if times else None


# --------------------------------------------------------------------------- protobuf (schema-less)

def _varint(b: bytes, i: int):
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7
        if s >= 70:
            raise ValueError("varint too long")


def pb_parse(b: bytes) -> list:
    """Decode one protobuf message into [(field_number, wire_type, value)]; raises ValueError."""
    out, i, n = [], 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if fn == 0 or fn > 536870911:
            raise ValueError("bad field number")
        if wt == 0:
            v, i = _varint(b, i)
        elif wt in (1, 5):
            size = 8 if wt == 1 else 4
            if i + size > n:
                raise ValueError("truncated fixed")
            v = int.from_bytes(b[i:i + size], "little")
            i += size
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > n:
                raise ValueError("truncated bytes")
            v = bytes(b[i:i + ln])
            i += ln
        else:
            raise ValueError("unsupported wire type")
        out.append((fn, wt, v))
    return out


def pb_all(buf, fn: int) -> list:
    if not isinstance(buf, (bytes, bytearray)):
        return []
    try:
        return [v for f, _, v in pb_parse(buf) if f == fn]
    except ValueError:
        return []


def pb_first(buf, *path):
    for fn in path:
        vals = pb_all(buf, fn)
        if not vals:
            return None
        buf = vals[0]
    return buf


def pb_str(buf, *path) -> str | None:
    v = pb_first(buf, *path)
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else None


def pb_ts(buf) -> float | None:
    secs = pb_first(buf, 1)
    if not isinstance(secs, int) or secs <= 0:
        return None
    nanos = pb_first(buf, 2)
    return secs + (nanos / 1e9 if isinstance(nanos, int) else 0)


def _looks_text(s: str) -> bool:
    return all(c.isprintable() or c in "\n\r\t" for c in s)


def _pb_value(v: bytes, depth: int):
    if not v:
        return ""
    try:
        s = v.decode("utf-8")
    except UnicodeDecodeError:
        s = None
    text_like = s is not None and _looks_text(s)
    # Bytes are ambiguous without a schema: the same payload may be a string or a nested message.
    # Real Antigravity messages nest < 15 levels and use field numbers < 200, so the limits below
    # (depth 40, field 1000) only reject text that happens to parse as protobuf. A printable
    # string is reinterpreted as a message only when it starts like a low-numbered (<= 32)
    # varint/bytes field and parses cleanly, e.g. {12: "file:///..."}.
    try_msg = depth < 40 and (not text_like or (len(v) >= 3 and v[0] < 0x80 and (v[0] & 7) in (0, 2) and v[0] >> 3 <= 32))
    if try_msg:
        try:
            fields = pb_parse(v)
            if fields and all(fn < 1000 for fn, _, _ in fields):
                return _pb_obj(fields, depth + 1)
        except ValueError:
            pass
    if s is not None:
        return s
    return {"bytes_base64": base64.b64encode(v).decode("ascii")}


def _pb_obj(fields: list, depth: int) -> dict:
    obj: dict = {}
    for fn, wt, v in fields:
        val = _pb_value(v, depth) if wt == 2 else v
        k = str(fn)
        if k not in obj:
            obj[k] = val
        elif isinstance(obj[k], list):
            obj[k].append(val)
        else:
            obj[k] = [obj[k], val]
    return obj


def pb_to_json(b: bytes):
    """Best-effort JSON view of a protobuf blob keyed by field number."""
    if not b:
        return None
    if b[:1] in (b"{", b"["):
        try:
            return json.loads(b.decode("utf-8"))
        except ValueError:
            pass
    try:
        return _pb_obj(pb_parse(b), 0)
    except ValueError:
        return {"bytes_base64": base64.b64encode(b).decode("ascii")}


# --------------------------------------------------------------------------- Claude Code

def claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else home() / ".claude") / "projects"


_CLAUDE_STRIP = re.compile(r"<(system-reminder|ide_opened_file)>.*?</\1>\s*", re.S)
_CLAUDE_HIDDEN = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                  "<bash-stdout>", "<bash-stderr>")


def claude_visible_text(text: str) -> str:
    """User text as shown in the transcript: slash commands rendered, injected context removed."""
    m = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if m:
        name = m.group(1).strip()
        a = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        if not name.startswith("/"):
            name = "/" + name
        return f"{name} {a.group(1).strip() if a else ''}".strip()
    m = re.fullmatch(r"\s*<bash-input>(.*?)</bash-input>\s*", text, re.S)
    if m:
        return "! " + m.group(1).strip()
    if text.lstrip().startswith(_CLAUDE_HIDDEN):
        return ""
    return _CLAUDE_STRIP.sub("", text).strip()


def _block_text(content) -> str:
    """Flatten Anthropic-style content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text") or "")
        elif b.get("type") in ("image", "document"):
            parts.append(f"[{b['type']}]")
        elif "content" in b:
            parts.append(_block_text(b["content"]))
    return "\n".join(p for p in parts if p)


def _head_tail_records(path: Path, size: int, chunk: int = 192 * 1024):
    """Records from the start (cwd, first prompt) and end (latest title) of a transcript.

    Listing must not read whole transcripts (they reach tens of MB); 192 KiB at each end covers the
    early metadata records and the periodically re-written title records.
    """
    with open(path, "rb") as f:
        head = f.read(chunk)
        tail = b""
        if size > chunk:
            f.seek(max(chunk, size - chunk))
            tail = f.read()
    complete = size <= chunk
    head_lines = head.split(b"\n")
    if not complete:
        head_lines = head_lines[:-1]
    tail_lines = tail.split(b"\n")[1:] if tail else []
    recs = []
    for line in head_lines + tail_lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            recs.append(r)
    return recs, complete


def _claude_info(f: Path, child: bool, parent: str | None = None) -> SessionInfo:
    st = f.stat()
    recs, complete = _head_tail_records(f, st.st_size)
    cwd = next((r["cwd"] for r in recs if r.get("cwd")), "")
    created = next((to_epoch(r["timestamp"]) for r in recs if r.get("timestamp")), None)
    titles = {}
    for r in recs:
        for t, k in (("custom-title", "customTitle"), ("ai-title", "aiTitle"), ("summary", "summary")):
            if r.get("type") == t and r.get(k):
                titles[t] = r[k]
    first_prompt, has_msgs = "", False
    for r in recs:
        if r.get("type") in ("user", "assistant"):
            has_msgs = True
        if r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary") \
                and (child or not r.get("isSidechain")):
            txt = claude_visible_text(_block_text((r.get("message") or {}).get("content")))
            if txt and r.get("toolUseResult") is None:  # tool results are user records too
                first_prompt = txt
                break
    if child and not parent:
        parent = next((r["sessionId"] for r in recs if r.get("sessionId") and r["sessionId"] != f.stem), None)
    title = titles.get("custom-title") or titles.get("ai-title") or titles.get("summary") or first_prompt
    return SessionInfo("claude", f.stem, str(f), title=short(title), projects=[cwd] if cwd else [],
                       created=created, updated=st.st_mtime, parent_id=parent, is_child=child,
                       empty=complete and not has_msgs)


def discover_claude(want, include_children: bool) -> list:
    root = claude_projects_dir()
    if not root.is_dir():
        return []
    out = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            child = f.stem.startswith("agent-")  # pre-2.x layout kept sidechains next to sessions
            if (child and not include_children) or not want(f.stem):
                continue
            out.append(_claude_info(f, child))
        if include_children:
            for f in proj.glob("*/subagents/*.jsonl"):
                if want(f.stem):
                    out.append(_claude_info(f, True, parent=f.parent.parent.name))
    return out


def claude_events(raw: list, main: bool = True) -> list:
    events, tool_names, seen = [], {}, set()
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            continue
        uid = r.get("uuid")
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        t, ts = r.get("type"), r.get("timestamp")
        side = bool(r.get("isSidechain")) and main  # sidechains are hidden only inside a main session
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        if t == "user":
            content = msg.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            texts, shown = [], []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_result":
                    cid = b.get("tool_use_id")
                    events.append(ev("tool_result", ts, i, role="tool", call_id=cid, name=tool_names.get(cid),
                                     output=_block_text(b.get("content")), is_error=b.get("is_error"),
                                     sidechain=side or None))
                elif bt == "text":
                    txt = b.get("text") or ""
                    texts.append(txt)
                    shown.append(claude_visible_text(txt))
                elif bt in ("image", "document"):
                    texts.append(f"[{bt}]")
                    shown.append(f"[{bt}]")
            if not texts:
                continue
            raw_text = "\n\n".join(texts)
            if r.get("isCompactSummary"):
                events.append(ev("event", ts, i, subtype="compact_summary", text=raw_text))
                continue
            visible = "\n\n".join(s for s in shown if s) if not (r.get("isMeta") or side) else ""
            events.append(ev("message", ts, i, role="user", text=visible or raw_text, visible=bool(visible),
                             meta=True if r.get("isMeta") else None, sidechain=side or None))
        elif t == "assistant":
            content = msg.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if r.get("isApiErrorMessage"):
                        events.append(ev("event", ts, i, subtype="api_error", text=b.get("text")))
                    else:
                        events.append(ev("message", ts, i, role="assistant", text=b.get("text") or "",
                                         visible=not side, model=msg.get("model"), sidechain=side or None))
                elif bt == "thinking":
                    events.append(ev("thinking", ts, i, role="assistant", text=b.get("thinking") or "",
                                     sidechain=side or None))
                elif bt == "redacted_thinking":
                    events.append(ev("thinking", ts, i, role="assistant", text="", redacted=True))
                elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                    tool_names[b.get("id")] = b.get("name")
                    events.append(ev("tool_call", ts, i, role="assistant", name=b.get("name"), call_id=b.get("id"),
                                     input=b.get("input"), sidechain=side or None))
                elif bt.endswith("tool_result"):
                    cid = b.get("tool_use_id")
                    events.append(ev("tool_result", ts, i, role="tool", call_id=cid, name=tool_names.get(cid),
                                     output=b.get("content"), is_error=b.get("is_error")))
                else:
                    events.append(ev("event", ts, i, subtype=f"assistant_{bt}", data=b))
        elif t == "attachment":
            a = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind", "human") == "human" \
                    and a.get("commandMode", "prompt") == "prompt":
                txt = claude_visible_text(_block_text(a.get("prompt")))
                if txt:
                    events.append(ev("message", ts or a.get("timestamp"), i, role="user", text=txt,
                                     visible=not side, queued=True))
        elif t == "system":
            skip = {"parentUuid", "isSidechain", "type", "uuid", "timestamp", "userType", "entrypoint", "cwd",
                    "sessionId", "session_id", "version", "gitBranch", "slug", "subtype", "content", "level"}
            data = {k: v for k, v in r.items() if k not in skip}
            events.append(ev("event", ts, i, subtype=r.get("subtype") or "system", text=r.get("content"),
                             level=r.get("level"), data=data or None))
    return events


def claude_meta(raw: list) -> dict:
    m: dict = {}
    models = set()
    for r in raw:
        if not isinstance(r, dict):
            continue
        t = r.get("type")
        if t == "custom-title" and r.get("customTitle"):
            m["custom_title"] = r["customTitle"]
        elif t == "ai-title" and r.get("aiTitle"):
            m["ai_title"] = r["aiTitle"]
        elif t == "summary" and r.get("summary"):
            m["summary"] = r["summary"]
        if r.get("cwd") and "cwd" not in m:
            m["cwd"] = r["cwd"]
        for k, mk in (("gitBranch", "git_branch"), ("version", "version"), ("entrypoint", "entrypoint")):
            if r.get(k):
                m[mk] = r[k]
        if t == "assistant":
            model = (r.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                models.add(model)
    m["models"] = sorted(models)
    return m


def load_claude(info: SessionInfo) -> Session:
    raw = read_jsonl(info.path)
    meta = claude_meta(raw)
    events = claude_events(raw, main=not info.is_child)
    subagents = []
    sub_dir = Path(info.path).with_suffix("") / "subagents"
    if not info.is_child and sub_dir.is_dir():
        for f in sorted(sub_dir.glob("*.jsonl")):
            sraw = read_jsonl(f)
            mf = f.with_suffix(".meta.json")
            smeta = None
            if mf.exists():
                try:
                    smeta = json.loads(mf.read_text(encoding="utf-8"))
                except ValueError:
                    smeta = None
            subagents.append({"id": f.stem, "path": str(f), "meta": smeta,
                              "events": claude_events(sraw, main=False), "raw": sraw})
    info.title = meta.get("custom_title") or meta.get("ai_title") or meta.get("summary") or info.title
    return Session(info, "claude-jsonl", meta, events, raw, extra={"subagents": subagents} if subagents else {})


# --------------------------------------------------------------------------- Codex

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex")


_CODEX_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$", re.I)
_CODEX_INJECTED = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<INSTRUCTIONS>",
                   "<permissions instructions>", "<user_shell_command>", "<turn_aborted>", "<skill>",
                   "<subagent_notification>", "<collaboration_mode>", "<personality_spec>", "<apps_instructions>")
_CODEX_EVENTS = {"task_started", "task_complete", "turn_aborted", "error", "stream_error", "warning",
                 "context_compacted", "entered_review_mode", "exited_review_mode", "plan_update",
                 "thread_rolled_back", "undo_completed", "background_event"}


def _latest_numbered(paths) -> Path | None:
    def num(p):
        d = re.findall(r"\d+", p.stem)
        return int(d[-1]) if d else -1
    paths = sorted(paths, key=num)
    return paths[-1] if paths else None


def _codex_threads(h: Path) -> dict:
    db = _latest_numbered(h.glob("state*.sqlite"))
    if not db:
        return {}
    try:
        con = open_sqlite(db)
        if "threads" not in sqlite_tables(con):
            return {}
        return {r["id"].lower(): dict(r) for r in con.execute("select * from threads")}
    except sqlite3.DatabaseError as e:
        warn(f"codex: cannot read {db}: {e}")
        return {}


def _codex_index_names(h: Path) -> dict:
    f = h / "session_index.jsonl"
    names = {}
    if f.exists():
        for r in iter_json_lines(f.read_text(encoding="utf-8", errors="replace")):
            if r.get("id") and r.get("thread_name"):
                names[r["id"].lower()] = r["thread_name"]
    return names


def _codex_source_parent(source):
    src = parse_jsonish(source)
    if isinstance(src, dict):
        sub = src.get("subagent")
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                return True, spawn.get("parent_thread_id")
        return "subagent" in src, None
    return False, None


def _codex_head_meta(p: Path) -> dict:
    """session_meta (or legacy header) of a rollout without a state-db row."""
    try:
        text = read_head(p).decode("utf-8", "replace")
    except (OSError, ReaderError):
        return {}
    first = next(iter_json_lines(text.split("\n", 1)[0]), {})
    meta = first.get("payload") if first.get("type") == "session_meta" else first
    meta = meta if isinstance(meta, dict) else {}
    if not meta.get("cwd"):
        m = re.search(r"<cwd>(.*?)</cwd>", text)
        if m:
            meta = dict(meta, cwd=m.group(1))
    return meta


def discover_codex(want, include_children: bool) -> list:
    h = codex_home()
    files: dict = {}
    for sub in ("sessions", "archived_sessions"):
        d = h / sub
        if not d.is_dir():
            continue
        for p in d.rglob("rollout-*.jsonl*"):
            m = _CODEX_ID.search(p.name)
            if not m or not want(m.group(1)):
                continue
            sid = m.group(1).lower()
            if sid in files and not files[sid].name.endswith(".zst"):
                continue  # keep the plain file while a compressed copy is being produced
            files[sid] = p
    if not files:
        return []
    threads, names = _codex_threads(h), _codex_index_names(h)
    out = []
    for sid, p in files.items():
        row = threads.get(sid)
        if row:
            child, parent = _codex_source_parent(row.get("source"))
            title = row.get("name") or names.get(sid) or row.get("title") or row.get("first_user_message") or ""
            cwd = row.get("cwd") or ""
            created = to_epoch(row.get("created_at_ms") or row.get("created_at"))
            signals = [row[k] for k in ("has_user_event", "first_user_message", "preview") if k in row]
            empty = bool(signals) and not any(signals)
        else:
            meta = _codex_head_meta(p)
            child, parent = _codex_source_parent(meta.get("source"))
            parent = meta.get("parent_thread_id") or parent
            child = child or bool(meta.get("parent_thread_id"))
            title, cwd = names.get(sid, ""), meta.get("cwd") or ""
            created, empty = to_epoch(meta.get("timestamp")), False
        if child and not include_children:
            continue
        out.append(SessionInfo("codex", sid, str(p), title=short(title), projects=[cwd] if cwd else [],
                               created=created, updated=mtime(p), parent_id=parent, is_child=child, empty=empty))
    return out


def _codex_output_text(out):
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        return _codex_output_text(out.get("content", out.get("output", json.dumps(out, ensure_ascii=False))))
    if isinstance(out, list):
        parts = []
        for c in out:
            if isinstance(c, dict):
                if "text" in c:
                    parts.append(c["text"])
                elif "image" in str(c.get("type", "")):
                    parts.append("[image]")
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return out


def _codex_texts(items) -> list:
    out = []
    for x in items or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and x.get("text"):
            out.append(x["text"])
    return out


def codex_item_events(item: dict, ts, i, include_reasoning: bool) -> list:
    """Events for an app-server ThreadItem (rollout item_completed or SQLite projection)."""
    t = str(item.get("type") or "")
    k = t[:1].lower() + t[1:]
    if k == "userMessage":
        parts = []
        for c in item.get("content") or []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                parts.append(c.get("text") or "")
            elif ct in ("image", "localImage", "local_image"):
                src = c.get("path") or c.get("url") or ""
                parts.append("[image]" if src.startswith("data:") or not src else f"[image: {src}]")
            elif ct in ("skill", "mention"):
                parts.append(f"[{ct}: {c.get('name') or c.get('path') or ''}]")
        return [ev("message", ts, i, role="user", text="\n\n".join(p for p in parts if p), visible=True, _src="ui")]
    if k == "agentMessage":
        text = item.get("text") or "\n".join(_codex_texts(item.get("content")))
        return [ev("message", ts, i, role="assistant", text=text, visible=True, phase=item.get("phase"), _src="ui")]
    if k == "reasoning":
        texts = _codex_texts(item.get("summary_text") or item.get("summary")) + \
            _codex_texts(item.get("raw_content") or item.get("content"))
        return [ev("thinking", ts, i, role="assistant", text="\n\n".join(texts))] if include_reasoning and texts else []
    cmd = item.get("command")
    if isinstance(cmd, list):
        cmd = cmd[2] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else shlex.join(str(c) for c in cmd)
    big = {"type", "id", "command", "cwd", "status", "aggregated_output", "aggregatedOutput", "exit_code", "exitCode"}
    return [ev("execution", ts, i, name=k, call_id=item.get("id"), command=cmd, cwd=file_uri_to_path(item.get("cwd")),
               status=item.get("status"), exit_code=item.get("exit_code", item.get("exitCode")),
               output=item.get("aggregated_output", item.get("aggregatedOutput")),
               data={kk: v for kk, v in item.items() if kk not in big} or None)]


def codex_events(raw: list):
    events, meta, call_names = [], {}, {}
    has_reasoning = any(isinstance(r, dict) and r.get("type") == "response_item"
                        and isinstance(r.get("payload"), dict) and r["payload"].get("type") == "reasoning"
                        for r in raw)
    for i, r in enumerate(raw):
        if not isinstance(r, dict) or "_unparsed" in r or r.get("record_type"):
            continue
        if "payload" in r and "type" in r:
            t, p, ts = r["type"], r["payload"], r.get("timestamp")
        elif i == 0 and "id" in r and "type" not in r:  # 2025 rollout header
            t, p, ts = "session_meta", r, r.get("timestamp")
        else:  # 2025 rollout body: bare ResponseItems
            t, p, ts = "response_item", r, None
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if t == "session_meta":
            for k in ("id", "cwd", "timestamp", "cli_version", "originator", "source", "thread_source",
                      "model_provider", "git", "forked_from_id", "parent_thread_id", "history_mode"):
                if p.get(k) is not None:
                    meta.setdefault(k, p[k])
        elif t == "turn_context":
            for k in ("model", "effort", "cwd", "approval_policy", "sandbox_policy"):
                if p.get(k) is not None:
                    meta[k if k != "cwd" else "last_cwd"] = p[k]
        elif t == "compacted":
            events.append(ev("event", ts, i, subtype="compacted", text=p.get("message")))
        elif t == "response_item":
            if pt == "message":
                parts = []
                for c in p.get("content") or []:
                    if isinstance(c, dict):
                        if c.get("text") is not None:
                            parts.append(c["text"])
                        elif "image" in str(c.get("type")):
                            parts.append("[image]")
                events.append(ev("message", ts, i, role=p.get("role"), text="\n\n".join(parts), visible=False,
                                 phase=p.get("phase"), _src="ri"))
            elif pt == "reasoning":
                texts = _codex_texts(p.get("summary")) + _codex_texts(p.get("content"))
                events.append(ev("thinking", ts, i, role="assistant", text="\n\n".join(texts),
                                 encrypted=True if (p.get("encrypted_content") and not p.get("content")) else None))
            elif pt and (pt.endswith("_call") or pt == "mcp_tool_call"):
                name = p.get("name") or pt[:-5]
                inp = p.get("arguments", p.get("input", p.get("action")))
                cid = p.get("call_id") or p.get("id")
                call_names[cid] = name
                events.append(ev("tool_call", ts, i, role="assistant", name=name, call_id=cid,
                                 input=parse_jsonish(inp), namespace=p.get("namespace"), status=p.get("status")))
            elif pt and pt.endswith("_output"):
                cid = p.get("call_id")
                events.append(ev("tool_result", ts, i, role="tool", call_id=cid, name=call_names.get(cid),
                                 output=_codex_output_text(p.get("output", p.get("tools")))))
            elif pt in ("compaction", "compaction_summary"):
                events.append(ev("event", ts, i, subtype="compaction"))
        elif t == "event_msg":
            if pt == "item_completed" and isinstance(p.get("item"), dict):
                events.extend(codex_item_events(p["item"], ts or p.get("completed_at_ms"), i, not has_reasoning))
            elif pt == "user_message":
                text = p.get("message") or ""
                imgs = [f"[image: {x}]" for x in p.get("local_images") or []] + ["[image]" for _ in p.get("images") or []]
                kind = p.get("kind")
                shown = kind in (None, "plain") and not text.lstrip().startswith(_CODEX_INJECTED)
                events.append(ev("message", ts, i, role="user", text="\n\n".join([text] + imgs).strip(),
                                 visible=shown, _src="legacy"))
            elif pt == "agent_message":
                events.append(ev("message", ts, i, role="assistant", text=p.get("message") or "", visible=True,
                                 phase=p.get("phase"), _src="legacy"))
            elif pt in ("agent_reasoning", "agent_reasoning_raw_content") and not has_reasoning:
                events.append(ev("thinking", ts, i, role="assistant", text=p.get("text") or ""))
            elif pt == "exec_command_end":
                cmd = p.get("command")
                if isinstance(cmd, list):
                    cmd = cmd[2] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else shlex.join(str(c) for c in cmd)
                out = p.get("aggregated_output") or "\n".join(x for x in (p.get("stdout"), p.get("stderr")) if x)
                events.append(ev("execution", ts, i, name="exec_command", call_id=p.get("call_id"), command=cmd,
                                 cwd=p.get("cwd"), exit_code=p.get("exit_code"), output=out))
            elif pt in ("patch_apply_end", "mcp_tool_call_end", "web_search_end"):
                data = {k: v for k, v in p.items() if k != "type"}
                events.append(ev("execution", ts, i, name=pt[:-4], call_id=p.get("call_id"), data=data))
            elif pt in _CODEX_EVENTS:
                data = {k: v for k, v in p.items() if k != "type"}
                events.append(ev("event", ts, i, subtype=pt, data=data or None))
    return codex_finalize(events), meta


def codex_finalize(events: list) -> list:
    """Pick the visible dialogue: UI-level items/events when present, else filtered ResponseItems."""
    ui = [e for e in events if e.get("_src") == "ui"]
    legacy = [e for e in events if e.get("_src") == "legacy"]
    drop = set()
    if ui or legacy:
        counts = collections.Counter((e["role"], e["text"].strip()) for e in ui)
        for e in legacy:  # sessions resumed across versions may record a message in both forms
            k = (e["role"], e["text"].strip())
            if counts[k] > 0:
                counts[k] -= 1
                drop.add(id(e))
        for e in events:
            if e.get("_src") == "ri" and e.get("role") in ("user", "assistant"):
                drop.add(id(e))
    else:
        for e in events:
            if e.get("_src") == "ri":
                text = e.get("text", "")
                e["visible"] = e.get("role") == "assistant" or (
                    e.get("role") == "user" and bool(text.strip()) and not text.lstrip().startswith(_CODEX_INJECTED))
    out = []
    for e in events:
        if id(e) in drop:
            continue
        e.pop("_src", None)
        out.append(e)
    return out


def _codex_projection(h: Path, sid: str):
    db = _latest_numbered(h.glob("thread_history*.sqlite"))
    if not db:
        return None
    con = open_sqlite(db)
    if "thread_items" not in sqlite_tables(con):
        return None
    rows = con.execute("select * from thread_items where thread_id=? order by rollout_ordinal", (sid,)).fetchall()
    if not rows:
        return None
    raw, events = [], []
    for i, r in enumerate(rows):
        rec = dict(r)
        rec["item_json"] = item = maybe_json(rec["item_json"])
        raw.append(rec)
        if isinstance(item, dict):
            events.extend(codex_item_events(item, rec.get("created_at_ms"), i, True))
    turns = []
    if "thread_turns" in sqlite_tables(con):
        turns = [dict(r) for r in con.execute(
            "select * from thread_turns where thread_id=? order by rollout_ordinal", (sid,))]
    return raw, codex_finalize(events), {"turns": turns}


def load_codex(info: SessionInfo) -> Session:
    h = codex_home()
    notes = []
    try:
        raw = read_jsonl(info.path)
        events, meta = codex_events(raw)
        storage = "codex-rollout-jsonl" + ("+zstd" if info.path.endswith(".zst") else "")
    except ZstdUnavailable as e:
        proj = _codex_projection(h, info.id)
        if proj is None:
            raise
        raw, events, meta = proj
        storage = "codex-thread-history-sqlite"
        notes.append(f"{e}. Fell back to the SQLite thread-history projection: UI-level items only "
                     "(no raw model requests / tool-call records).")
    row = _codex_threads(h).get(info.id)
    if row:
        meta["thread"] = row
        if row.get("name"):
            info.title = row["name"]
    return Session(info, storage, meta, events, raw, notes=notes)


# --------------------------------------------------------------------------- OpenCode

def opencode_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local" / "share") / "opencode"


def discover_opencode(want, include_children: bool) -> list:
    root = opencode_dir()
    out, seen = [], set()

    def add(d: dict, kind: str, path: str, has_msgs, locator: dict):
        sid = d.get("id")
        if not sid or sid in seen or not want(sid):
            return
        seen.add(sid)
        parent = d.get("parent_id") or d.get("parentID")
        if parent and not include_children:
            return
        t = d.get("time") if isinstance(d.get("time"), dict) else {}
        directory = d.get("directory") or ""
        out.append(SessionInfo(
            "opencode", sid, path, title=short(d.get("title") or ""), projects=[directory] if directory else [],
            created=to_epoch(d.get("time_created") or t.get("created")),
            updated=to_epoch(d.get("time_updated") or t.get("updated")),
            parent_id=parent, is_child=bool(parent), empty=has_msgs is False, locator=dict(locator, kind=kind)))

    for db in sorted(root.glob("opencode*.db")):
        try:
            con = open_sqlite(db)
            tbl = sqlite_tables(con)
            if "session_v2" in tbl:
                counts = dict(con.execute("select session_id, count(*) from session_message "
                                          "where type in ('user','assistant') group by 1").fetchall()) \
                    if "session_message" in tbl else {}
                for r in con.execute("select * from session_v2"):
                    add(dict(r), "v2", str(db), counts.get(r["id"], 0) > 0, {"db": str(db)})
            if "session" in tbl and "message" in tbl:
                counts = dict(con.execute("select session_id, count(*) from message group by 1").fetchall())
                for r in con.execute("select * from session"):
                    add(dict(r), "v1", str(db), counts.get(r["id"], 0) > 0, {"db": str(db)})
        except sqlite3.DatabaseError as e:
            warn(f"opencode: cannot read {db}: {e}")
    storage = root / "storage"
    for f in sorted((storage / "session").glob("*/*.json")) if (storage / "session").is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            msg_dir = storage / "message" / str(d.get("id"))
            add(d, "json", str(f), msg_dir.is_dir() and any(msg_dir.iterdir()), {"storage": str(storage)})
    return out


def _oc_tool_events(ts, i, name, call_id, state: dict, extra_out=None) -> list:
    state = state if isinstance(state, dict) else {}
    out = state.get("content")
    if isinstance(out, list):
        out = "\n".join(c.get("text", "") for c in out if isinstance(c, dict) and c.get("type") == "text")
    if out is None:
        out = state.get("output", extra_out)
    status = state.get("status")
    return [ev("tool_call", ts, i, role="assistant", name=name, call_id=call_id, input=state.get("input"),
               title=state.get("title")),
            ev("tool_result", ts, i, role="tool", name=name, call_id=call_id, output=out, status=status,
               is_error=True if status == "error" else None, error=state.get("error"),
               metadata=state.get("metadata"))]


def opencode_v2_events(raw: list) -> list:
    events = []
    for i, m in enumerate(raw):
        d = m.get("data") if isinstance(m.get("data"), dict) else {}
        t = m.get("type")
        ts = (d.get("time") or {}).get("created") or m.get("time_created")
        if t == "user":
            text = d.get("text") or ""
            atts = []
            for f in d.get("files") or []:
                if not isinstance(f, dict):
                    continue
                name, mime = f.get("name") or f.get("filename") or "file", f.get("mime")
                mention = (f.get("mention") or {}).get("text")
                atts.append({k: v for k, v in f.items() if k != "data"})
                if not (mention and mention in text):
                    text += f"\n\n[attachment: {name}{f' ({mime})' if mime else ''}]"
            events.append(ev("message", ts, i, role="user", text=text.strip(), visible=bool(text.strip()),
                             attachments=atts or None))
        elif t == "assistant":
            content = d.get("content")
            if not content and m.get("parts"):
                content = [dict(p["data"], type=p["data"].get("type", p.get("type"))) if isinstance(p.get("data"), dict)
                           else {"type": p.get("type"), "data": p.get("data")} for p in m["parts"]]
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                ct, cts = c.get("type"), (c.get("time") or {}).get("created") or ts
                if ct == "text":
                    events.append(ev("message", cts, i, role="assistant", text=c.get("text") or "", visible=True))
                elif ct == "reasoning":
                    events.append(ev("thinking", cts, i, role="assistant", text=c.get("text") or ""))
                elif ct == "tool":
                    events.extend(_oc_tool_events(cts, i, c.get("name") or c.get("tool"),
                                                  c.get("id") or c.get("callID"), c.get("state")))
                else:
                    events.append(ev("event", cts, i, subtype=str(ct), data=c))
            if d.get("error"):
                events.append(ev("event", ts, i, subtype="error", data=d["error"]))
        elif t == "synthetic":
            events.append(ev("message", ts, i, role="user", text=d.get("text") or "", visible=False, synthetic=True))
        else:
            data = {k: v for k, v in d.items() if k not in ("time", "text")}
            events.append(ev("event", ts, i, subtype=str(t), text=d.get("text"), data=data or None))
    return events


def opencode_v1_events(raw: list) -> list:
    """Events for OpenCode 1.x data: [{"info": message, "parts": [part, ...]}]."""
    events = []
    for i, m in enumerate(raw):
        info, parts = m.get("info") or {}, m.get("parts") or []
        role = info.get("role")
        ts = (info.get("time") or {}).get("created")
        texts = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            pts = (p.get("time") or {}).get("start") or ts
            if pt == "text":
                hidden = p.get("synthetic") or p.get("ignored")
                if role == "user" and not hidden:
                    texts.append(p.get("text") or "")
                else:
                    events.append(ev("message", pts, i, role=role, text=p.get("text") or "", visible=not hidden,
                                     synthetic=True if p.get("synthetic") else None))
            elif pt == "file" and role == "user":
                texts.append(f"[attachment: {p.get('filename') or 'file'}{' (' + p['mime'] + ')' if p.get('mime') else ''}]")
            elif pt == "reasoning":
                events.append(ev("thinking", pts, i, role="assistant", text=p.get("text") or ""))
            elif pt == "tool":
                st = p.get("state") or {}
                events.extend(_oc_tool_events((st.get("time") or {}).get("start") or ts, i, p.get("tool"),
                                              p.get("callID"), st))
            else:
                events.append(ev("event", pts, i, subtype=str(pt), data=p))
        if texts:
            events.append(ev("message", ts, i, role="user", text="\n\n".join(texts).strip(), visible=True))
        if info.get("error"):
            events.append(ev("event", ts, i, subtype="error", data=info["error"]))
    return events


def load_opencode(info: SessionInfo) -> Session:
    kind, sid = info.locator.get("kind"), info.id
    meta: dict = {}
    if kind in ("v1", "v2"):
        con = open_sqlite(info.locator["db"])
        tbl = sqlite_tables(con)
        srow = con.execute(f"select * from {'session_v2' if kind == 'v2' else 'session'} where id=?", (sid,)).fetchone()
        meta = {k: maybe_json(v) for k, v in dict(srow or {}).items()}
        if "project" in tbl and meta.get("project_id"):
            prow = con.execute("select * from project where id=?", (meta["project_id"],)).fetchone()
            if prow:
                meta["project"] = {k: maybe_json(v) for k, v in dict(prow).items() if "icon" not in k}
        if kind == "v2":
            parts: dict = {}
            if "session_part" in tbl:
                for r in con.execute("select * from session_part where session_id=? order by message_id, ordinal", (sid,)):
                    parts.setdefault(r["message_id"], []).append(dict(r, data=maybe_json(r["data"])))
            raw = []
            for r in con.execute("select * from session_message where session_id=? order by seq", (sid,)):
                rec = dict(r, data=maybe_json(r["data"]))
                if r["id"] in parts:
                    rec["parts"] = parts[r["id"]]
                raw.append(rec)
            return Session(info, "opencode-sqlite-v2", meta, opencode_v2_events(raw), raw)
        parts = {}
        for r in con.execute("select * from part where message_id in (select id from message where session_id=?) "
                             "order by id", (sid,)):
            d = maybe_json(r["data"])
            d = dict(d, id=r["id"], messageID=r["message_id"]) if isinstance(d, dict) else {"data": d}
            parts.setdefault(r["message_id"], []).append(d)
        raw = []
        for r in con.execute("select * from message where session_id=? order by time_created, id", (sid,)):
            d = maybe_json(r["data"])
            d = dict(d, id=r["id"]) if isinstance(d, dict) else {"id": r["id"], "data": d}
            d.setdefault("time", {"created": r["time_created"]})
            raw.append({"info": d, "parts": parts.get(r["id"], [])})
        return Session(info, "opencode-sqlite-v1", meta, opencode_v1_events(raw), raw)
    storage = Path(info.locator["storage"])
    meta = json.loads(Path(info.path).read_text(encoding="utf-8"))

    def load_dir(d: Path) -> list:
        items = []
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return items

    msgs = sorted(load_dir(storage / "message" / sid), key=lambda x: ((x.get("time") or {}).get("created") or 0, x.get("id", "")))
    raw = [{"info": m, "parts": sorted(load_dir(storage / "part" / str(m.get("id"))), key=lambda p: p.get("id", ""))}
           for m in msgs]
    return Session(info, "opencode-json-storage", meta, opencode_v1_events(raw), raw)


# --------------------------------------------------------------------------- Antigravity (agy)

AGY_STEP_TYPES = {14: "user_input", 15: "planner_response", 17: "error_message", 23: "checkpoint",
                  101: "system_message", 132: "tool"}


def agy_roots() -> list:
    base = home() / ".gemini"
    return [base / "antigravity-cli", base / "antigravity"]


def _agy_summaries(root: Path) -> dict:
    db = root / "conversation_summaries.db"
    if not db.exists():
        return {}
    try:
        con = open_sqlite(db)
        if "conversation_summaries" not in sqlite_tables(con):
            return {}
        return {r["conversation_id"]: dict(r) for r in con.execute("select * from conversation_summaries")}
    except sqlite3.DatabaseError as e:
        warn(f"agy: cannot read {db}: {e}")
        return {}


def _agy_db_meta(db: Path) -> dict:
    """Workspace, creation time and first prompt straight from a conversation db."""
    out: dict = {}
    try:
        con = open_sqlite(db)
        tbl = sqlite_tables(con)
        if "trajectory_metadata_blob" in tbl:
            row = con.execute("select data from trajectory_metadata_blob limit 1").fetchone()
            if row and row[0]:
                b = row[0]
                ws = [file_uri_to_path(pb_str(w, 1)) for w in pb_all(b, 1)] or [file_uri_to_path(pb_str(b, 7))]
                out["projects"] = [w for w in ws if w]
                out["created"] = pb_ts(pb_first(b, 2))
        if "steps" in tbl:
            out["steps"] = con.execute("select count(*) from steps").fetchone()[0]
            row = con.execute("select step_payload from steps where step_type=14 order by idx limit 1").fetchone()
            if row and row[0]:
                out["first_prompt"] = pb_str(row[0], 19, 2) or ""
            row = con.execute("select step_payload from steps order by idx desc limit 1").fetchone()
            if row and row[0]:
                md = pb_first(row[0], 5)
                out["last_step"] = max(filter(None, (pb_ts(pb_first(md, f)) for f in (1, 6, 7, 8, 32))), default=None)
    except sqlite3.DatabaseError:
        pass
    return out


def discover_agy(want, include_children: bool) -> list:
    out = []
    for root in agy_roots():
        conv = root / "conversations"
        if not conv.is_dir():
            continue
        sums = _agy_summaries(root)
        for db in conv.glob("*.db"):
            sid = db.stem
            if not want(sid):
                continue
            s = sums.get(sid) or {}
            parent = s.get("parent_conversation_id") or None
            child = bool(parent) or (s.get("nesting_depth") or 0) > 0
            if child and not include_children:
                continue
            m = _agy_db_meta(db)
            projects = [file_uri_to_path(u) for u in (maybe_json(s.get("workspace_uris")) or [])
                        if isinstance(u, str)] or m.get("projects") or []
            # the CLI touches every db file on startup, so file mtimes are not activity times
            updated = to_epoch(s.get("last_modified_time")) or m.get("last_step") or mtime(db, str(db) + "-wal")
            steps = s.get("step_count") if s else m.get("steps")
            out.append(SessionInfo("agy", sid, str(db), title=short(s.get("title") or m.get("first_prompt") or ""),
                                   projects=projects, created=m.get("created"), updated=updated,
                                   parent_id=parent, is_child=child, empty=steps == 0,
                                   locator={"root": str(root)}))
    return out


def agy_events(steps: list) -> list:
    events, tool_names = [], {}
    for i, r in enumerate(steps):
        p, t = r.get("step_payload") or b"", r.get("step_type")
        ts = pb_ts(pb_first(p, 5, 1))
        if t == 14:
            u = pb_first(p, 19)
            text = pb_str(u, 2) or "\n".join(filter(None, (pb_str(x, 1) for x in pb_all(u, 3))))
            imgs = [{"mime": pb_str(x, 1), "name": pb_str(x, 4), "path": pb_str(x, 5)} for x in pb_all(u, 9)]
            for im in imgs:
                text += f"\n\n[image: {im['path'] or im['name'] or im['mime']}]"
            events.append(ev("message", ts, i, role="user", text=text.strip(), visible=bool(text.strip()),
                             attachments=imgs or None))
        elif t == 15:
            resp = pb_first(p, 20)
            thinking = pb_str(resp, 3)
            if thinking:
                events.append(ev("thinking", ts, i, role="assistant", text=thinking))
            text = pb_str(resp, 8) or pb_str(resp, 1)
            if text:
                events.append(ev("message", ts, i, role="assistant", text=text, visible=True))
            for call in pb_all(resp, 7):
                cid, name = pb_str(call, 1), pb_str(call, 2)
                tool_names[cid] = name
                events.append(ev("tool_call", ts, i, role="assistant", name=name, call_id=cid,
                                 input=parse_jsonish(pb_str(call, 3))))
        elif t == 132:
            call, res = pb_first(p, 5, 4), pb_first(p, 140)
            cid = pb_str(call, 1)
            err = pb_str(p, 31, 2) or pb_str(p, 31, 3)
            bg = pb_first(p, 148)
            background = {"task_id": pb_str(bg, 1), "command": pb_str(bg, 4), "description": pb_str(bg, 7),
                          "log": file_uri_to_path(pb_str(bg, 2)), "output": pb_str(bg, 3)} if bg else None
            events.append(ev("tool_result", pb_ts(pb_first(p, 5, 8)) or ts, i, role="tool", call_id=cid,
                             name=pb_str(call, 2) or tool_names.get(cid), output=pb_str(res, 2, 1),
                             is_error=True if err else None, error=err, status=r.get("status"),
                             background_task={k: v for k, v in background.items() if v} if background else None))
        elif t == 17:
            e = pb_first(p, 24, 3)
            events.append(ev("event", ts, i, subtype="error", text=pb_str(e, 1), detail=pb_str(e, 2),
                             data={"raw_error": pb_str(e, 3), "body": pb_str(e, 5), "status_code": pb_first(e, 7)}))
        elif t == 23:
            events.append(ev("event", ts, i, subtype="checkpoint", text=pb_str(p, 30, 5)))
        elif t == 101:
            events.append(ev("event", ts, i, subtype="system_message", text=pb_str(p, 114, 1),
                             sender=pb_str(p, 114, 4, 3)))
        else:
            body = {k: v for k, v in (pb_to_json(p) or {}).items() if k not in ("1", "4", "5")} if p else None
            events.append(ev("event", ts, i, subtype=f"step_{t}", data=body or None))
    return events


def load_agy(info: SessionInfo) -> Session:
    con = open_sqlite(info.path)
    tbl = sqlite_tables(con)
    steps = [dict(r) for r in con.execute("select * from steps order by idx")] if "steps" in tbl else []
    raw = []
    for r in steps:
        rec = {k: v for k, v in r.items() if not isinstance(v, (bytes, bytearray))}
        rec["step_type_name"] = AGY_STEP_TYPES.get(r.get("step_type"), f"step_{r.get('step_type')}")
        for k, v in r.items():
            if isinstance(v, (bytes, bytearray)) and v:
                rec[k] = pb_to_json(v)
        raw.append(rec)
    meta: dict = {}
    raw_tables: dict = {}
    if "trajectory_meta" in tbl:
        meta["trajectory_meta"] = [dict(r) for r in con.execute("select * from trajectory_meta")]
    for name in ("trajectory_metadata_blob", "gen_metadata", "executor_metadata", "parent_references", "battle_mode_infos"):
        if name in tbl:
            rows = [{k: (pb_to_json(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in dict(r).items()}
                    for r in con.execute(f"select * from {name}")]
            if rows:  # small trajectory metadata stays in metadata; per-generation tables are raw data
                (meta if name == "trajectory_metadata_blob" else raw_tables)[name] = rows
    models = []
    for r in con.execute("select data from gen_metadata") if "gen_metadata" in tbl else []:
        for v in (pb_str(r[0], 1, 21), pb_str(r[0], 1, 19)):
            if v and v not in models:
                models.append(v)
    meta["models"] = models
    summary = _agy_summaries(Path(info.locator.get("root") or Path(info.path).parent.parent)).get(info.id)
    if summary:
        summary["raw_summary"] = pb_to_json(summary.get("raw_summary") or b"")
        meta["summary"] = summary
    return Session(info, "agy-conversation-sqlite-protobuf", meta, agy_events(steps), raw,
                   extra={"raw_tables": raw_tables} if raw_tables else {})


# --------------------------------------------------------------------------- locating sessions

DISCOVER = {"claude": discover_claude, "codex": discover_codex, "opencode": discover_opencode, "agy": discover_agy}
LOAD = {"claude": load_claude, "codex": load_codex, "opencode": load_opencode, "agy": load_agy}


def normalize_agent(name: str) -> str:
    a = AGENT_ALIASES.get(name.strip().lower())
    if not a:
        raise ReaderError(f"unknown agent {name!r}; choose from: {', '.join(AGENTS)}", 2)
    return a


def discover(agents, include_children: bool, want=None) -> list:
    want = want or (lambda _sid: True)
    found = []
    for a in agents:
        try:
            found.extend(DISCOVER[a](want, include_children))
        except Exception as e:  # one broken store must not hide the others
            warn(f"{a}: discovery failed: {e}")
    return found


def current_session_ids() -> dict:
    return {a: {os.environ[v] for v in envs if os.environ.get(v)} for a, envs in CURRENT_SESSION_ENV.items()}


def parse_when(s: str, end: bool = False) -> float:
    """today / yesterday / now / 3d / 12h / 30m / 2w / YYYY-MM-DD[THH:MM[:SS]] (local time)."""
    v = s.strip().lower()
    now = dt.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = None
    if v == "now":
        return now.timestamp()
    if v == "today":
        day = midnight
    elif v == "yesterday":
        day = midnight - dt.timedelta(days=1)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", v)
    if m:
        secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return now.timestamp() - float(m.group(1)) * secs
    if day is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        day = dt.datetime.strptime(v, "%Y-%m-%d").astimezone()
    if day is not None:
        return (day + dt.timedelta(days=1)).timestamp() if end else day.timestamp()
    try:
        d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReaderError(f"cannot parse time {s!r} (try: today, yesterday, 3d, 12h, 2026-11-17, 2026-11-17T09:00)", 2)
    return (d if d.tzinfo else d.astimezone()).timestamp()


def candidates(args, agents) -> list:
    since = parse_when(args.since) if args.since else None
    until = parse_when(args.until, end=True) if args.until else None
    out = []
    for s in discover(agents, include_children=args.all):
        if not args.all and (s.is_child or s.empty):
            continue
        if args.project and not project_match(s.projects, args.project):
            continue
        if since is not None and (s.updated or 0) < since:
            continue
        if until is not None and (s.created or s.updated or 0) >= until:
            continue
        out.append(s)
    out.sort(key=lambda s: s.updated or 0, reverse=True)
    return out


def resolve_id(args, agents) -> SessionInfo:
    q = args.id.strip().lower()
    found = discover(agents, include_children=True, want=lambda sid: sid.lower().startswith(q))
    exact = [s for s in found if s.id.lower() == q]
    cands = exact or found
    if not cands:
        raise ReaderError(f"no {'/'.join(agents)} session with id or prefix {args.id!r}")
    if len(cands) > 1:
        cands.sort(key=lambda s: s.updated or 0, reverse=True)
        raise ReaderError(f"id prefix {args.id!r} is ambiguous; use a longer prefix or --agent:\n"
                          + render_list(cands[:20], {}), 2)
    return cands[0]


def pick_session(args, agents) -> Session:
    """--id, or the most recent candidate that actually contains dialogue."""
    if args.id:
        return load(resolve_id(args, agents))
    cands = candidates(args, agents)
    if not args.include_current:
        cur = current_session_ids()
        skipped = [s for s in cands if s.id in cur.get(s.agent, ())]
        if skipped:
            cands = [s for s in cands if s not in skipped]
            warn(f"skipped the current {AGENT_NAMES[skipped[0].agent]} session {skipped[0].id} "
                 "(pass --include-current to read it)")
    if not cands:
        filters = [f"agent={'/'.join(agents)}"] + [f"{k}={getattr(args, k)}" for k in ("project", "since", "until")
                                                    if getattr(args, k)]
        raise ReaderError(f"no session found ({', '.join(filters)}); try --list or --all")
    fallback = None
    # Storage metadata cannot always tell that a session is empty (e.g. opened and closed without a
    # prompt), so load candidates until one has dialogue; 10 bounds the cost of a run of empty ones.
    for info in cands[:10]:
        try:
            s = load(info)
        except ReaderError as e:
            warn(f"skipping {info.agent} session {info.id}: {e}")
            continue
        if args.all or visible_turns(s.events):
            return s
        fallback = fallback or s
    if fallback is None:
        raise ReaderError("none of the most recent matching sessions could be read")
    return fallback


def load(info: SessionInfo) -> Session:
    s = LOAD[info.agent](info)
    stamps = [to_epoch(e["timestamp"]) for e in s.events if e.get("timestamp")]
    if stamps:
        info.created = info.created or min(stamps)
        info.updated = max(info.updated or 0, max(stamps))
    if not info.title:
        first = next((e["text"] for e in s.events if e["type"] == "message" and e.get("visible")
                      and e.get("role") == "user"), "")
        info.title = short(first)
    return s


# --------------------------------------------------------------------------- output

def visible_turns(events: list) -> list:
    turns = []
    for e in events:
        if e.get("type") != "message" or not e.get("visible") or e.get("role") not in ("user", "assistant"):
            continue
        text = (e.get("text") or "").strip("\n")
        if not text.strip():
            continue
        if turns and turns[-1]["role"] == e["role"]:
            turns[-1]["texts"].append(text)
        else:
            turns.append({"role": e["role"], "timestamp": e.get("timestamp"), "texts": [text]})
    return turns


def render_markdown(s: Session) -> str:
    info = s.info
    lines = [f"# {info.title or 'Untitled session'}", "",
             f"- Agent: {AGENT_NAMES[info.agent]}", f"- Session ID: `{info.id}`"]
    if info.projects:
        lines.append(f"- Project: `{'`, `'.join(info.projects)}`")
    if info.created:
        lines.append(f"- Started: {local_time(info.created, tz=True)}")
    if info.updated:
        lines.append(f"- Last activity: {local_time(info.updated, tz=True)}")
    if info.parent_id:
        lines.append(f"- Parent session: `{info.parent_id}`")
    lines.append(f"- Source: `{info.path}`")
    lines += [f"- Note: {n}" for n in s.notes]
    lines += ["", "---", ""]
    turns = visible_turns(s.events)
    # "## [n] Role · time" stays distinguishable from headings inside the messages themselves
    for n, t in enumerate(turns, 1):
        when = local_time(to_epoch(t["timestamp"])) if t["timestamp"] else ""
        lines += [f"## [{n}] {'User' if t['role'] == 'user' else 'Assistant'}" + (f" · {when}" if when else ""), "",
                  "\n\n".join(t["texts"]), ""]
    if not turns:
        lines.append("_No visible user/assistant messages in this session._")
    return "\n".join(lines).rstrip() + "\n"


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return {"bytes_base64": base64.b64encode(o).decode("ascii")}
    return str(o)


def render_json(s: Session, include_raw: bool = True) -> str:
    info = s.info
    counts = collections.Counter(e["type"] for e in s.events)
    doc = {
        "schema": "session-reader/1",
        "agent": info.agent,
        "agent_name": AGENT_NAMES[info.agent],
        "session_id": info.id,
        "title": info.title,
        "project": info.project,
        "projects": info.projects,
        "created_at": iso(info.created),
        "updated_at": iso(info.updated),
        "parent_id": info.parent_id,
        "is_subagent": info.is_child,
        "source": {"path": info.path, "storage": s.storage},
        "notes": s.notes,
        "stats": {"events": len(s.events), "visible_messages": sum(1 for e in s.events if e.get("visible")),
                  **dict(sorted(counts.items())), "raw_records": len(s.raw)},
        "metadata": s.meta,
        "events": s.events,
    }
    if include_raw:
        doc["raw"] = s.raw
    for k, v in s.extra.items():
        if not include_raw:
            if k.startswith("raw"):
                continue
            if k == "subagents":
                v = [{kk: vv for kk, vv in sub.items() if kk != "raw"} for sub in v]
        doc[k] = v
    return json.dumps(doc, ensure_ascii=False, indent=2, default=_json_default) + "\n"


def render_list(infos: list, current: dict) -> str:
    if not infos:
        return "No sessions found.\n"
    rows = [("AGENT", "SESSION ID", "LAST ACTIVITY", "PROJECT", "TITLE")]
    for s in infos:
        proj = pretty_path(s.project)
        if len(proj) > 40:
            proj = "…" + proj[-39:]
        tags = []
        if s.id in current.get(s.agent, ()):
            tags.append("(current)")
        if s.is_child:
            tags.append(f"(subagent of {s.parent_id})" if s.parent_id else "(subagent)")
        if s.empty:
            tags.append("(empty)")
        title = " ".join([short(s.title, 70)] + tags)
        rows.append((s.agent, s.id, local_time(s.updated)[:16], proj, title))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    return "\n".join("  ".join(r[c].ljust(widths[c]) for c in range(4)) + "  " + r[4] for r in rows) + "\n"


def write_output(text: str, output: str | None, label: str) -> None:
    if output:
        p = Path(output).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        warn(f"wrote {label} to {p} ({len(text.encode('utf-8'))} bytes)")
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


# --------------------------------------------------------------------------- CLI

EPILOG = """examples:
  %(prog)s --agent agy --latest                 # most recent agy session as Markdown
  %(prog)s --id 3f2a9c1e                        # agent auto-detected from id / id prefix
  %(prog)s --latest --project .                 # latest session of any agent in this project
  %(prog)s --list --agent claude --since yesterday --until yesterday
  %(prog)s --agent codex --latest --format json --no-raw -o /tmp/codex.json
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session_reader.py", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Read local sessions of Claude Code, Codex, OpenCode and Antigravity (agy). Read-only.")
    p.add_argument("--agent", "-a", help="claude | codex | opencode | agy (aliases: claude-code, cc, oc, antigravity)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--id", help="session id or unique id prefix; agent is auto-detected unless --agent is given")
    mode.add_argument("--latest", action="store_true", help="most recently active matching session (the default)")
    mode.add_argument("--list", action="store_true", help="list matching sessions, newest first")
    p.add_argument("--project", "-p", help="only sessions whose working directory is PATH or inside it ('.' = cwd)")
    p.add_argument("--since", help="last activity at/after WHEN: today, yesterday, 3d, 12h, 2026-11-17, 2026-11-17T09:00")
    p.add_argument("--until", help="started before WHEN; a date (or today/yesterday) includes that whole day")
    p.add_argument("--format", "-f", choices=("markdown", "md", "json"), default="markdown",
                   help="markdown: visible dialogue (default); json: full session data")
    p.add_argument("--output", "-o", help="write to this file instead of stdout")
    p.add_argument("--no-raw", action="store_true", help="json: omit original records (normalized events are kept)")
    p.add_argument("--limit", "-n", type=int, default=20, help="--list: number of rows (0 = all, default 20)")
    p.add_argument("--all", action="store_true", help="also consider subagent/child and empty sessions")
    p.add_argument("--include-current", action="store_true",
                   help="let --latest return the session this command is running inside")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    fmt = "json" if args.format == "json" else "markdown"
    try:
        agents = [normalize_agent(args.agent)] if args.agent else list(AGENTS)
        if args.list:
            infos = candidates(args, agents)
            if args.limit > 0:
                infos = infos[: args.limit]
            if fmt == "json":
                text = json.dumps([s.to_dict() for s in infos], ensure_ascii=False, indent=2) + "\n"
            else:
                text = render_list(infos, current_session_ids())
            write_output(text, args.output, "session list")
            return 0
        session = pick_session(args, agents)
        text = render_json(session, include_raw=not args.no_raw) if fmt == "json" else render_markdown(session)
        write_output(text, args.output, f"{fmt} for {session.info.agent} session {session.info.id}")
        return 0
    except ReaderError as e:
        warn(str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
