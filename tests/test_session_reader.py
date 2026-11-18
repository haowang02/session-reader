"""Tests for skills/session-reader/scripts/session_reader.py against synthetic stores of all four agents.

Run:  python3 -m unittest discover -s tests -v      (or: python3 -m pytest tests)
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "session-reader" / "scripts" / "session_reader.py"
_spec = importlib.util.spec_from_file_location("session_reader", SCRIPT)
sr = importlib.util.module_from_spec(_spec)
sys.modules["session_reader"] = sr  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(sr)

ENV_KEYS = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")

CC_MAIN = "11111111-1111-4111-8111-111111111111"
CC_OLD = "22222222-2222-4222-8222-222222222222"
CC_EMPTY = "33333333-3333-4333-8333-333333333333"
CX_NEW = "019a0000-0000-7000-8000-000000000001"
CX_LEGACY = "019a0000-0000-7000-8000-000000000002"
CX_BARE = "019a0000-0000-7000-8000-000000000003"
CX_GUARD = "019a0000-0000-7000-8000-000000000004"
CX_ZST = "019a0000-0000-7000-8000-000000000005"
OC_V2 = "ses_v2aaaaaaaaaaaaaaaaaaaaaaaa"
OC_V2_CHILD = "ses_v2childbbbbbbbbbbbbbbbbbbb"
OC_V1 = "ses_v1cccccccccccccccccccccccc"
OC_JSON = "ses_jsondddddddddddddddddddddd"
AG_MAIN = "aaaaaaaa-0000-4000-8000-000000000001"
AG_CHILD = "aaaaaaaa-0000-4000-8000-000000000002"
AG_ZERO = "aaaaaaaa-0000-4000-8000-000000000003"


def epoch(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def iso(s: str) -> str:
    return s.replace(" ", "T") + ".000Z"


def write_jsonl(path: Path, records, when: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    if when:
        os.utime(path, (epoch(when), epoch(when)))


# ---- minimal protobuf encoder for AGY fixtures

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def pb(*fields) -> bytes:
    out = b""
    for fn, v in fields:
        if isinstance(v, int):
            out += _varint(fn << 3) + _varint(v)
        else:
            v = v.encode() if isinstance(v, str) else v
            out += _varint(fn << 3 | 2) + _varint(len(v)) + v
    return out


def pb_time(s: str) -> bytes:
    return pb((1, int(epoch(s))), (2, 500_000_000))


# --------------------------------------------------------------------------- fixtures

def build_claude(home: Path, proj: Path, other: Path):
    pdir = home / ".claude" / "projects" / "-work-proj"
    n = [0]

    def rec(type_, ts, sid=CC_MAIN, cwd=proj, **kw):
        n[0] += 1
        r = {"type": type_, "timestamp": iso(ts), "uuid": f"u{n[0]}", "parentUuid": f"u{n[0] - 1}",
             "sessionId": sid, "cwd": str(cwd), "isSidechain": False, "version": "2.1.0"}
        r.update(kw)
        return r

    main = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": CC_MAIN},
        rec("user", "2026-11-18 09:00:00", message={"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>SECRET-CONTEXT</system-reminder>"},
            {"type": "text", "text": "Please design a cache layer.\n\n```py\nx = 1\n```"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}}]}),
        rec("assistant", "2026-11-18 09:00:05", message={"id": "m1", "role": "assistant", "model": "claude-test",
            "content": [{"type": "thinking", "thinking": "THINKING-ABOUT-SQLITE", "signature": "sig"}]}),
        rec("assistant", "2026-11-18 09:00:06", message={"id": "m1", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "Let me check the repo."}]}),
        rec("assistant", "2026-11-18 09:00:07", message={"id": "m1", "role": "assistant", "model": "claude-test",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}]}),
        rec("user", "2026-11-18 09:00:08", toolUseResult={"stdout": "TOOL-OUTPUT-FILE"},
            message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                  "content": "TOOL-OUTPUT-FILE", "is_error": False}]}),
        rec("assistant", "2026-11-18 09:00:09", message={"id": "m2", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "Use SQLite because it is embedded."}]}),
        rec("user", "2026-11-18 09:01:00", isMeta=True, message={"role": "user", "content":
            "<local-command-caveat>CAVEAT-TEXT</local-command-caveat>"}),
        rec("user", "2026-11-18 09:01:01", message={"role": "user", "content":
            "<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>opus</command-args>"}),
        rec("user", "2026-11-18 09:01:02", message={"role": "user", "content":
            "<local-command-stdout>LOCAL-STDOUT</local-command-stdout>"}),
        rec("attachment", "2026-11-18 09:01:03", attachment={"type": "queued_command", "prompt": "also add tests",
            "commandMode": "prompt", "origin": {"kind": "human"}}),
        rec("attachment", "2026-11-18 09:01:03", attachment={"type": "total_tokens_reminder", "total": 1}),
        rec("system", "2026-11-18 09:01:04", subtype="api_error", level="error", content="API-ERROR-TEXT"),
        rec("user", "2026-11-18 09:02:00", isCompactSummary=True, message={"role": "user", "content":
            "COMPACT-SUMMARY-TEXT"}),
        rec("assistant", "2026-11-18 09:02:30", isSidechain=True, message={"role": "assistant",
            "content": [{"type": "text", "text": "SIDECHAIN-TEXT"}]}),
        rec("user", "2026-11-18 09:03:00", message={"role": "user", "content": "<bash-input>git status</bash-input>"}),
        rec("assistant", "2026-11-18 09:03:10", message={"role": "assistant", "content": [
            {"type": "text", "text": "Done. Tests added."}]}),
        {"type": "ai-title", "aiTitle": "Cache layer design", "sessionId": CC_MAIN},
    ]
    write_jsonl(pdir / f"{CC_MAIN}.jsonl", main, "2026-11-18 10:00:00")
    sub = [rec("user", "2026-11-18 09:00:20", isSidechain=True, agentId="abc", message={"role": "user", "content": "SUB-TASK"}),
           rec("assistant", "2026-11-18 09:00:30", isSidechain=True, agentId="abc", message={"role": "assistant",
               "content": [{"type": "text", "text": "SUB-RESULT"}]})]
    write_jsonl(pdir / CC_MAIN / "subagents" / "agent-abc.jsonl", sub, "2026-11-18 09:00:30")
    (pdir / CC_MAIN / "subagents" / "agent-abc.meta.json").write_text(json.dumps({"agentType": "Explore"}))
    old = [rec("user", "2026-11-10 08:00:00", sid=CC_OLD, cwd=other, message={"role": "user", "content": "old question"}),
           rec("assistant", "2026-11-10 08:00:10", sid=CC_OLD, cwd=other, message={"role": "assistant",
               "content": [{"type": "text", "text": "old answer"}]})]
    write_jsonl(home / ".claude" / "projects" / "-work-other" / f"{CC_OLD}.jsonl", old, "2026-11-10 08:00:10")
    write_jsonl(pdir / f"{CC_EMPTY}.jsonl", [{"type": "permission-mode", "sessionId": CC_EMPTY}], "2026-11-18 12:00:00")


def build_codex(home: Path, proj: Path) -> Path:
    cx = home / ".codex"
    day = cx / "sessions" / "2026" / "11" / "17"

    def w(ts, type_, payload):
        return {"timestamp": iso(ts), "type": type_, "payload": payload}

    new = [
        w("2026-11-17 08:00:00", "session_meta", {"id": CX_NEW, "cwd": str(proj), "cli_version": "0.170.0",
                                                  "source": "cli", "timestamp": iso("2026-11-17 08:00:00"),
                                                  "base_instructions": {"text": "BASE"}}),
        w("2026-11-17 08:00:01", "turn_context", {"model": "gpt-test", "cwd": str(proj)}),
        w("2026-11-17 08:00:01", "response_item", {"type": "message", "role": "developer",
                                                   "content": [{"type": "input_text", "text": "DEV-PROMPT"}]}),
        w("2026-11-17 08:00:01", "response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>AGENTS-BODY</INSTRUCTIONS>"}]}),
        w("2026-11-17 08:00:02", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        w("2026-11-17 08:00:02", "response_item", {"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": "Why SQLite?"}]}),
        w("2026-11-17 08:00:02", "event_msg", {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "UserMessage", "id": "i1", "content": [{"type": "text", "text": "Why SQLite?", "text_elements": []}]}}),
        w("2026-11-17 08:00:03", "response_item", {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "REASONING-SUMMARY"}], "encrypted_content": "gAAA"}),
        w("2026-11-17 08:00:03", "event_msg", {"type": "item_completed", "item": {
            "type": "Reasoning", "id": "r1", "summary_text": ["REASONING-SUMMARY"], "raw_content": []}}),
        w("2026-11-17 08:00:04", "response_item", {"type": "function_call", "name": "exec_command",
                                                   "arguments": "{\"cmd\": \"ls\"}", "call_id": "c1"}),
        w("2026-11-17 08:00:05", "event_msg", {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "e1", "command": ["/bin/zsh", "-lc", "ls"], "cwd": f"file://{proj}",
            "status": "completed", "aggregated_output": "CMD-OUTPUT", "exit_code": 0}}),
        w("2026-11-17 08:00:05", "response_item", {"type": "function_call_output", "call_id": "c1", "output": "CMD-OUTPUT"}),
        w("2026-11-17 08:00:06", "response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                                   "content": [{"type": "output_text", "text": "SQLite is embedded."}]}),
        w("2026-11-17 08:00:06", "event_msg", {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "a1", "phase": "final_answer",
            "content": [{"type": "Text", "text": "SQLite is embedded."}]}}),
        w("2026-11-17 08:00:07", "event_msg", {"type": "token_count", "info": {"total": 10}}),
        w("2026-11-17 08:00:07", "event_msg", {"type": "task_complete", "turn_id": "t1",
                                               "last_agent_message": "SQLite is embedded."}),
    ]
    write_jsonl(day / f"rollout-2026-11-17T16-00-00-{CX_NEW}.jsonl", new, "2026-11-17 08:00:07")

    legacy = [
        w("2026-11-12 08:00:00", "session_meta", {"id": CX_LEGACY, "cwd": str(proj / "sub"), "source": "cli",
                                                  "timestamp": iso("2026-11-12 08:00:00")}),
        w("2026-11-12 08:00:01", "response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"}]}),
        w("2026-11-12 08:00:01", "event_msg", {"type": "user_message", "message": "<environment_context>ENV</environment_context>",
                                               "kind": "environment_context"}),
        w("2026-11-12 08:00:02", "response_item", {"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": "Legacy question"}]}),
        w("2026-11-12 08:00:02", "event_msg", {"type": "user_message", "message": "Legacy question", "kind": "plain",
                                               "images": []}),
        w("2026-11-12 08:00:03", "event_msg", {"type": "agent_reasoning", "text": "LEGACY-THINKING"}),
        w("2026-11-12 08:00:04", "response_item", {"type": "local_shell_call", "call_id": "s1", "status": "completed",
                                                   "action": {"type": "exec", "command": ["ls"]}}),
        w("2026-11-12 08:00:04", "event_msg", {"type": "exec_command_end", "call_id": "s1", "command": ["bash", "-lc", "ls"],
                                               "stdout": "LEGACY-OUT", "stderr": "", "exit_code": 0}),
        w("2026-11-12 08:00:05", "response_item", {"type": "message", "role": "assistant",
                                                   "content": [{"type": "output_text", "text": "Legacy answer"}]}),
        w("2026-11-12 08:00:05", "event_msg", {"type": "agent_message", "message": "Legacy answer"}),
    ]
    write_jsonl(cx / "sessions" / "2026" / "11" / "12" / f"rollout-2026-11-12T16-00-00-{CX_LEGACY}.jsonl",
                legacy, "2026-11-12 08:00:05")

    bare = [
        {"id": CX_BARE, "timestamp": "2025-04-01T08:00:00.000Z", "instructions": None},
        {"record_type": "state"},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"<environment_context>\n<cwd>{proj}</cwd>\n</environment_context>"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Bare question"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Bare answer"}]},
    ]
    write_jsonl(cx / "sessions" / "2025" / "04" / "01" / f"rollout-2025-04-01T16-00-00-{CX_BARE}.jsonl",
                bare, "2025-04-01 08:00:00")

    guard = [w("2026-11-18 11:00:00", "session_meta", {"id": CX_GUARD, "cwd": str(proj),
                                                       "source": {"subagent": {"other": "guardian"}}}),
             w("2026-11-18 11:00:01", "event_msg", {"type": "item_completed", "item": {
                 "type": "AgentMessage", "content": [{"type": "Text", "text": "{\"outcome\":\"allow\"}"}]}})]
    write_jsonl(cx / "sessions" / "2026" / "11" / "18" / f"rollout-2026-11-18T19-00-00-{CX_GUARD}.jsonl",
                guard, "2026-11-18 11:00:01")

    zst_records = [w("2026-11-16 08:00:00", "session_meta", {"id": CX_ZST, "cwd": str(proj), "source": "vscode"}),
                   w("2026-11-16 08:00:01", "event_msg", {"type": "item_completed", "item": {
                       "type": "UserMessage", "content": [{"type": "text", "text": "ZST question"}]}}),
                   w("2026-11-16 08:00:02", "event_msg", {"type": "item_completed", "item": {
                       "type": "AgentMessage", "content": [{"type": "Text", "text": "ZST answer"}]}})]
    plain = "".join(json.dumps(r) + "\n" for r in zst_records).encode()
    zst_path = cx / "sessions" / "2026" / "11" / "16" / f"rollout-2026-11-16T16-00-00-{CX_ZST}.jsonl.zst"
    zst_path.parent.mkdir(parents=True, exist_ok=True)
    zst_path.write_bytes(_zstd_compress(plain) or (sr.ZSTD_MAGIC + b"not-really-zstd"))
    os.utime(zst_path, (epoch("2026-11-16 08:00:02"),) * 2)

    con = sqlite3.connect(cx / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text, created_at integer, updated_at integer,"
                " source text, cwd text, title text, has_user_event integer, first_user_message text, name text,"
                " created_at_ms integer, model text)")
    con.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (CX_NEW, str(day / f"rollout-2026-11-17T16-00-00-{CX_NEW}.jsonl"), int(epoch("2026-11-17 08:00:00")),
                 int(epoch("2026-11-17 08:00:07")), "cli", str(proj), "Why SQLite?", 0, "Why SQLite?",
                 "SQLite discussion", int(epoch("2026-11-17 08:00:00") * 1000), "gpt-test"))
    con.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (CX_GUARD, "x", 0, 0, '{"subagent":{"other":"guardian"}}', str(proj), "", 0, "", None, 0, None))
    con.commit()
    con.close()

    con = sqlite3.connect(cx / "thread_history_1.sqlite")
    con.execute("create table thread_items (thread_id text, turn_id text, item_id text, rollout_ordinal integer,"
                " created_at_ms integer, item_json text, item_type text)")
    con.execute("create table thread_turns (thread_id text, turn_id text, rollout_ordinal integer, status text)")
    items = [{"type": "userMessage", "id": "p1", "content": [{"type": "text", "text": "ZST question"}]},
             {"type": "commandExecution", "id": "p2", "command": "/bin/zsh -lc pwd", "aggregatedOutput": "/w",
              "exitCode": 0, "status": "completed"},
             {"type": "agentMessage", "id": "p3", "text": "ZST answer"}]
    for i, it in enumerate(items):
        con.execute("insert into thread_items values (?,?,?,?,?,?,?)",
                    (CX_ZST, "t1", it["id"], i, int(epoch("2026-11-16 08:00:01") * 1000) + i, json.dumps(it), it["type"]))
    con.execute("insert into thread_turns values (?,?,?,?)", (CX_ZST, "t1", 0, "completed"))
    con.commit()
    con.close()
    (cx / "session_index.jsonl").write_text(json.dumps({"id": CX_LEGACY, "thread_name": "Legacy chat"}) + "\n")
    return zst_path


def _zstd_compress(data: bytes) -> bytes | None:
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if exe:
        return subprocess.run([exe, "-q", "-c"], input=data, capture_output=True, check=True).stdout
    return None


def build_opencode(home: Path, proj: Path, other: Path):
    base = home / ".local" / "share" / "opencode"
    base.mkdir(parents=True)
    ms = lambda s: int(epoch(s) * 1000)  # noqa: E731
    con = sqlite3.connect(base / "opencode.db")
    con.executescript("""
        create table project (id text primary key, worktree text, name text, icon_url text);
        create table session_v2 (id text primary key, project_id text, parent_id text, slug text, directory text,
            title text, version text, time_created integer, time_updated integer, metadata text);
        create table session_message (id text primary key, session_id text, type text, seq integer,
            time_created integer, time_updated integer, data text);
        create table session_part (message_id text, session_id text, ordinal integer, type text, call_id text,
            time_created integer, time_updated integer, data text);
    """)
    con.execute("insert into project values ('p1', ?, 'proj', 'data:icon')", (str(proj),))
    con.execute("insert into session_v2 values (?,?,?,?,?,?,?,?,?,?)",
                (OC_V2, "p1", None, "calm-river", str(proj), "OC v2 chat", "2.3.2", ms("2026-11-16 08:00:00"),
                 ms("2026-11-16 08:05:00"), '{"k": 1}'))
    con.execute("insert into session_v2 values (?,?,?,?,?,?,?,?,?,?)",
                (OC_V2_CHILD, "p1", OC_V2, "sub", str(proj), "child", "2.3.2", ms("2026-11-16 08:01:00"),
                 ms("2026-11-18 23:00:00"), None))
    msgs = [
        ("m1", OC_V2, "user", 1, {"time": {"created": ms("2026-11-16 08:00:00")}, "text": "[Image 1] How to add an index?",
                                  "files": [{"mime": "image/png", "name": "clipboard", "data": "iVBORw0KGgo",
                                             "source": {"type": "inline"}, "mention": {"text": "[Image 1]"}}]}),
        ("m2", OC_V2, "assistant", 2, {"time": {"created": ms("2026-11-16 08:00:01")}, "model": {"id": "m"}, "content": [
            {"type": "reasoning", "text": "OC-THINKING"},
            {"type": "tool", "id": "call_1", "name": "shell", "state": {"status": "completed", "input": {"command": "ls"},
                                                                       "content": [{"type": "text", "text": "OC-TOOL-OUT"}]}},
            {"type": "text", "text": "Use CREATE INDEX."}]}),
        ("m3", OC_V2, "idle", 3, {"time": {"created": ms("2026-11-16 08:00:02")}, "outcome": "succeeded"}),
        ("m4", OC_V2, "synthetic", 4, {"time": {"created": ms("2026-11-16 08:00:03")}, "text": "SYNTHETIC-CONTINUE"}),
        ("m5", OC_V2, "assistant", 5, {"time": {"created": ms("2026-11-16 08:00:04")}, "content": [],
                                       "error": {"type": "provider.auth", "message": "Invalid API key"}}),
        ("m6", OC_V2_CHILD, "user", 1, {"time": {"created": ms("2026-11-16 08:01:00")}, "text": "child task"}),
    ]
    for mid, sid, t, seq, data in msgs:
        con.execute("insert into session_message values (?,?,?,?,?,?,?)",
                    (mid, sid, t, seq, data["time"]["created"], data["time"]["created"], json.dumps(data)))
    con.commit()
    con.close()

    con = sqlite3.connect(base / "opencode-v1.db")
    con.executescript("""
        create table session (id text primary key, project_id text, parent_id text, slug text, directory text,
            title text, version text, time_created integer, time_updated integer);
        create table message (id text primary key, session_id text, time_created integer, time_updated integer, data text);
        create table part (id text primary key, message_id text, session_id text, time_created integer,
            time_updated integer, data text);
    """)
    con.execute("insert into session values (?,?,?,?,?,?,?,?,?)",
                (OC_V1, "p1", None, "s", str(other), "OC v1 chat", "1.2.0", ms("2026-11-01 08:00:00"), ms("2026-11-01 08:10:00")))
    con.execute("insert into message values ('msg_01', ?, ?, ?, ?)", (OC_V1, ms("2026-11-01 08:00:00"), 0,
                json.dumps({"role": "user", "time": {"created": ms("2026-11-01 08:00:00")}})))
    con.execute("insert into message values ('msg_02', ?, ?, ?, ?)", (OC_V1, ms("2026-11-01 08:00:05"), 0,
                json.dumps({"role": "assistant", "time": {"created": ms("2026-11-01 08:00:05")}, "modelID": "x"})))
    parts = [("prt_01", "msg_01", {"type": "text", "text": "v1 question"}),
             ("prt_02", "msg_01", {"type": "text", "text": "V1-SYNTHETIC", "synthetic": True}),
             ("prt_03", "msg_02", {"type": "step-start"}),
             ("prt_04", "msg_02", {"type": "reasoning", "text": "V1-THINKING"}),
             ("prt_05", "msg_02", {"type": "tool", "callID": "c1", "tool": "bash", "state": {
                 "status": "error", "input": {"command": "false"}, "error": "V1-TOOL-ERROR"}}),
             ("prt_06", "msg_02", {"type": "text", "text": "v1 answer"}),
             ("prt_07", "msg_02", {"type": "step-finish", "reason": "stop"})]
    for pid, mid, data in parts:
        con.execute("insert into part values (?,?,?,?,?,?)", (pid, mid, OC_V1, 0, 0, json.dumps(data)))
    con.commit()
    con.close()

    st = base / "storage"
    (st / "session" / "proj1").mkdir(parents=True)
    (st / "session" / "proj1" / f"{OC_JSON}.json").write_text(json.dumps({
        "id": OC_JSON, "projectID": "proj1", "directory": str(other), "title": "OC json chat", "version": "0.15.0",
        "time": {"created": ms("2026-10-01 08:00:00"), "updated": ms("2026-10-01 08:10:00")}}))
    for mid, info, prts in [
        ("msg_a", {"role": "user", "time": {"created": ms("2026-10-01 08:00:00")}},
         [{"id": "prt_a1", "type": "text", "text": "json question"},
          {"id": "prt_a2", "type": "file", "mime": "text/plain", "filename": "notes.txt", "url": "file:///n"}]),
        ("msg_b", {"role": "assistant", "time": {"created": ms("2026-10-01 08:00:05")}},
         [{"id": "prt_b1", "type": "tool", "callID": "c9", "tool": "read",
           "state": {"status": "completed", "input": {"filePath": "/n"}, "output": "JSON-TOOL-OUT"}},
          {"id": "prt_b2", "type": "text", "text": "json answer"}]),
    ]:
        (st / "message" / OC_JSON).mkdir(parents=True, exist_ok=True)
        (st / "message" / OC_JSON / f"{mid}.json").write_text(json.dumps(dict(info, id=mid, sessionID=OC_JSON)))
        (st / "part" / mid).mkdir(parents=True, exist_ok=True)
        for p in prts:
            (st / "part" / mid / f"{p['id']}.json").write_text(json.dumps(dict(p, messageID=mid, sessionID=OC_JSON)))


def build_agy(home: Path, proj: Path):
    root = home / ".gemini" / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)

    def conv(cid: str, steps):
        con = sqlite3.connect(root / "conversations" / f"{cid}.db")
        con.executescript("""
            create table trajectory_meta (trajectory_id text primary key, cascade_id text, trajectory_type integer, source integer);
            create table steps (idx integer primary key, step_type integer, status integer, has_subtrajectory numeric,
                metadata blob, error_details blob, permissions blob, task_details blob, render_info blob,
                step_payload blob, step_format integer);
            create table trajectory_metadata_blob (id text primary key, data blob);
            create table gen_metadata (idx integer primary key, data blob, size integer);
        """)
        con.execute("insert into trajectory_meta values ('traj-1', ?, 4, 17)", (cid,))
        con.execute("insert into trajectory_metadata_blob values ('main', ?)",
                    (pb((1, pb((1, f"file://{proj}"))), (2, pb_time("2026-11-18 08:00:00")), (6, cid)),))
        con.execute("insert into gen_metadata values (0, ?, 0)", (pb((1, pb((19, "gemini-test"), (21, "Gemini Test")))),))
        for idx, (stype, status, payload) in enumerate(steps):
            con.execute("insert into steps (idx, step_type, status, has_subtrajectory, step_payload, step_format) "
                        "values (?,?,?,0,?,0)", (idx, stype, status, payload))
        con.commit()
        con.close()

    def meta(ts, extra=()):
        return (5, pb((1, pb_time(ts)), *extra))

    call = pb((1, "call_1"), (2, "run_command"), (3, '{"CommandLine":"ls -la"}'))
    conv(AG_MAIN, [
        (14, 3, pb((1, 14), (4, 3), meta("2026-11-18 08:00:00"), (19, pb(
            (2, "AGY question"), (3, pb((1, "AGY question"))),
            (9, pb((1, "image/png"), (2, b"\x89PNG\r\n"), (4, "clipboard"), (5, "/tmp/shot.png"))))))),
        (15, 3, pb((1, 15), (4, 3), meta("2026-11-18 08:00:01"), (20, pb((3, "AGY-THINKING"), (6, "bot-1"), (7, call))))),
        (132, 3, pb((1, 132), (4, 3), meta("2026-11-18 08:00:02", ((4, call),)),
                    (140, pb((1, pb((1, "CommandLine"), (2, "ls -la"))), (2, pb((1, "AGY-TOOL-OUT"))))))),
        (132, 7, pb((1, 132), (4, 7), meta("2026-11-18 08:00:03", ((4, pb((1, "call_2"), (2, "read_file"))),)),
                    (31, pb((2, "AGY-TOOL-ERROR"))), (140, pb((2, pb((1, ""))))))),
        (15, 3, pb((1, 15), (4, 3), meta("2026-11-18 08:00:04"), (20, pb((1, "AGY answer"), (8, "AGY answer"), (6, "b"))))),
        (17, 3, pb((1, 17), (4, 3), meta("2026-11-18 08:00:05"), (24, pb((3, pb((1, "AGY-ERROR"), (2, "INVALID_ARGUMENT"))))))),
        (101, 3, pb((1, 101), (4, 3), meta("2026-11-18 08:00:06"), (114, pb((1, "AGY-SYSTEM-MSG"), (4, pb((3, "system"))))))),
        (999, 3, pb((1, 999), (4, 3), meta("2026-11-18 08:00:07"), (200, pb((1, "FUTURE-STEP"))))),
    ])
    conv(AG_CHILD, [(14, 3, pb((1, 14), meta("2026-11-18 08:30:00"), (19, pb((2, "child prompt")))))])
    conv(AG_ZERO, [(14, 3, pb((1, 14), meta("2026-11-11 08:00:00"), (19, pb((2, "zero-time prompt"))))),
                   (15, 3, pb((1, 15), meta("2026-11-11 08:00:01"), (20, pb((1, "zero-time answer")))))])
    con = sqlite3.connect(root / "conversation_summaries.db")
    con.execute("create table conversation_summaries (conversation_id text primary key, title text, preview text,"
                " step_count integer, last_modified_time datetime, workspace_uris text, status text,"
                " parent_conversation_id text, nesting_depth integer, raw_summary blob)")
    rows = [(AG_MAIN, "AGY Title", "", 8, "2026-11-18 09:00:00.123456+00:00", json.dumps([f"file://{proj}"]), "IDLE",
             "", 0, pb((1, "summary"))),
            (AG_CHILD, "Child", "", 1, "2026-11-18 23:30:00+00:00", "[]", "IDLE", AG_MAIN, 1, None),
            (AG_ZERO, "Zero time", "", 2, "0001-01-01 00:00:00+00:00", "[]", "IDLE", "", 0, None)]
    con.executemany("insert into conversation_summaries values (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class SessionReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sr-test-"))
        cls.home = cls.tmp / "home"
        cls.proj = cls.tmp / "work" / "proj"
        cls.other = cls.tmp / "work" / "other"
        for d in (cls.home, cls.proj / "sub", cls.other):
            d.mkdir(parents=True)
        build_claude(cls.home, cls.proj, cls.other)
        cls.zst_path = build_codex(cls.home, cls.proj)
        build_opencode(cls.home, cls.proj, cls.other)
        build_agy(cls.home, cls.proj)
        cls.zstd_ok = _zstd_compress(b"x") is not None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sr.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def md(self, *args) -> str:
        code, out, err = self.cli(*args)
        self.assertEqual(code, 0, err)
        return out

    def js(self, *args) -> dict:
        code, out, err = self.cli(*args, "--format", "json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    @staticmethod
    def events(doc, type_=None):
        return [e for e in doc["events"] if type_ is None or e["type"] == type_]

    # ------------------------------------------------------------- locating

    def test_list_covers_all_agents_and_hides_children_and_empty(self):
        doc = self.js("--list", "--limit", "0")
        ids = [s["id"] for s in doc]
        for sid in (CC_MAIN, CC_OLD, CX_NEW, CX_LEGACY, CX_BARE, OC_V2, OC_V1, OC_JSON, AG_MAIN, AG_ZERO):
            self.assertIn(sid, ids)
        for sid in (CC_EMPTY, CX_GUARD, OC_V2_CHILD, AG_CHILD, "agent-abc"):
            self.assertNotIn(sid, ids)
        self.assertEqual(ids[0], CC_MAIN, "newest first")
        all_ids = [s["id"] for s in self.js("--list", "--all", "--limit", "0")]
        for sid in (CX_GUARD, OC_V2_CHILD, AG_CHILD, "agent-abc", CC_EMPTY):
            self.assertIn(sid, all_ids)
        table = self.md("--list", "--agent", "claude")
        self.assertIn("Cache layer design", table)

    def test_latest_per_agent(self):
        expect = {"claude": CC_MAIN, "codex": CX_NEW, "opencode": OC_V2, "agy": AG_MAIN,
                  "cc": CC_MAIN, "antigravity": AG_MAIN, "oc": OC_V2}
        for agent, sid in expect.items():
            self.assertEqual(self.js("--agent", agent, "--latest", "--no-raw")["session_id"], sid, agent)
        self.assertEqual(self.js("--no-raw")["session_id"], CC_MAIN, "latest is the default mode")

    def test_latest_skips_current_session(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = CC_MAIN
        code, out, err = self.cli("--agent", "claude", "--latest")
        self.assertEqual(code, 0)
        self.assertIn("old question", out)
        self.assertIn("skipped the current", err)
        self.assertIn("Cache layer design", self.md("--agent", "claude", "--include-current"))
        self.assertEqual(self.js("--no-raw")["session_id"], AG_MAIN)
        self.assertEqual(self.js("--id", CC_MAIN, "--no-raw")["session_id"], CC_MAIN, "--id is never skipped")
        self.assertIn("(current)", self.md("--list", "--agent", "claude"))

    def test_id_exact_prefix_and_autodetect(self):
        cases = {CC_MAIN: "claude", CX_NEW: "codex", OC_V1: "opencode", AG_MAIN: "agy", "agent-abc": "claude"}
        for q, agent in cases.items():
            doc = self.js("--id", q, "--no-raw")
            self.assertEqual(doc["agent"], agent, q)
        self.assertEqual(self.js("--id", "11111111", "--no-raw")["session_id"], CC_MAIN)
        self.assertEqual(self.js("--id", CX_NEW.upper(), "--no-raw")["session_id"], CX_NEW, "case-insensitive")
        code, _, err = self.cli("--id", "019a0000")
        self.assertEqual(code, 2)
        self.assertIn("ambiguous", err)
        code, _, err = self.cli("--id", "deadbeef")
        self.assertEqual(code, 1)
        code, _, _ = self.cli("--agent", "codex", "--id", CC_MAIN)
        self.assertEqual(code, 1, "--agent restricts the id search")
        code, _, err = self.cli("--agent", "cursor")
        self.assertEqual(code, 2)

    def test_project_filter(self):
        self.assertEqual(self.js("--latest", "--project", str(self.proj), "--no-raw")["session_id"], CC_MAIN)
        self.assertEqual(self.js("--agent", "codex", "--project", str(self.proj), "--no-raw")["session_id"], CX_NEW)
        sub = [s["id"] for s in self.js("--list", "--project", str(self.proj / "sub"))]
        self.assertEqual(sub, [CX_LEGACY])
        other = {s["id"] for s in self.js("--list", "--project", str(self.other))}
        self.assertEqual(other, {CC_OLD, OC_V1, OC_JSON})
        old_cwd = os.getcwd()
        try:
            os.chdir(self.proj)
            self.assertEqual(self.js("--agent", "opencode", "--project", ".", "--no-raw")["session_id"], OC_V2)
        finally:
            os.chdir(old_cwd)

    def test_time_filters(self):
        ids = {s["id"] for s in self.js("--list", "--since", "2026-11-17", "--until", "2026-11-17")}
        self.assertEqual(ids, {CX_NEW})
        ids = {s["id"] for s in self.js("--list", "--since", "2026-11-18")}
        self.assertEqual(ids, {CC_MAIN, AG_MAIN})
        self.assertEqual({s["id"] for s in self.js("--list", "--until", "2025-12-31")}, {CX_BARE})
        self.assertEqual(self.cli("--list", "--since", "last tuesday")[0], 2)
        now = datetime.now().timestamp()
        self.assertAlmostEqual(sr.parse_when("2d"), now - 2 * 86400, delta=5)
        self.assertLess(sr.parse_when("yesterday"), sr.parse_when("today"))
        self.assertEqual(sr.parse_when("today", end=True) - sr.parse_when("today"), 86400)

    # ------------------------------------------------------------- Claude

    def test_claude_markdown_only_visible_dialogue(self):
        out = self.md("--id", CC_MAIN)
        for s in ("# Cache layer design", "Please design a cache layer.\n\n```py\nx = 1\n```", "[image]",
                  "Let me check the repo.", "Use SQLite because it is embedded.", "/model opus", "also add tests",
                  "! git status", "Done. Tests added.", f"`{self.proj}`"):
            self.assertIn(s, out)
        for s in ("SECRET-CONTEXT", "THINKING-ABOUT-SQLITE", "ls -la", "TOOL-OUTPUT-FILE", "CAVEAT-TEXT",
                  "LOCAL-STDOUT", "API-ERROR-TEXT", "COMPACT-SUMMARY-TEXT", "SIDECHAIN-TEXT", "SUB-RESULT"):
            self.assertNotIn(s, out)
        heads = [line for line in out.splitlines() if line.startswith("## [")]
        self.assertEqual([h.split(" ")[2] for h in heads],
                         ["User", "Assistant", "User", "Assistant"], "consecutive same-role messages merge")
        self.assertTrue(heads[0].startswith("## [1] User · "))
        self.assertLess(out.index("Let me check the repo."), out.index("Use SQLite because"))

    def test_claude_json_full_record(self):
        doc = self.js("--id", CC_MAIN)
        self.assertEqual(doc["source"]["storage"], "claude-jsonl")
        raw_lines = (self.home / ".claude/projects/-work-proj" / f"{CC_MAIN}.jsonl").read_text().strip().split("\n")
        self.assertEqual(doc["raw"], [json.loads(x) for x in raw_lines], "raw records preserved verbatim")
        self.assertEqual(self.events(doc, "thinking")[0]["text"], "THINKING-ABOUT-SQLITE")
        call, = self.events(doc, "tool_call")
        self.assertEqual((call["name"], call["input"], call["call_id"]), ("Bash", {"command": "ls -la"}, "toolu_1"))
        res, = self.events(doc, "tool_result")
        self.assertEqual((res["name"], res["output"], res["is_error"]), ("Bash", "TOOL-OUTPUT-FILE", False))
        subtypes = {e.get("subtype") for e in self.events(doc, "event")}
        self.assertTrue({"api_error", "compact_summary"} <= subtypes)
        hidden = [e for e in self.events(doc, "message") if not e["visible"]]
        self.assertTrue(any("CAVEAT-TEXT" in e["text"] for e in hidden))
        self.assertTrue(any(e.get("sidechain") and "SIDECHAIN-TEXT" in e["text"] for e in hidden))
        self.assertEqual(doc["metadata"]["models"], ["claude-test"])
        sub, = doc["subagents"]
        self.assertEqual((sub["id"], sub["meta"]), ("agent-abc", {"agentType": "Explore"}))
        self.assertIn("SUB-RESULT", [e.get("text") for e in sub["events"]])
        raw_e = self.events(doc)[0]
        self.assertEqual(doc["raw"][raw_e["raw_index"]]["uuid"], "u1")
        lean = self.js("--id", CC_MAIN, "--no-raw")
        self.assertNotIn("raw", lean)
        self.assertNotIn("raw", lean["subagents"][0])
        self.assertEqual(len(lean["events"]), len(doc["events"]))

    def test_claude_subagent_readable_directly(self):
        out = self.md("--id", "agent-abc")
        self.assertIn("SUB-TASK", out)
        self.assertIn("SUB-RESULT", out)
        self.assertIn(f"Parent session: `{CC_MAIN}`", out)

    # ------------------------------------------------------------- Codex

    def test_codex_current_format(self):
        out = self.md("--id", CX_NEW)
        self.assertIn("# SQLite discussion", out, "title from state db")
        self.assertEqual(out.count("Why SQLite?"), 1)
        self.assertEqual(out.count("SQLite is embedded."), 1)
        for s in ("AGENTS-BODY", "DEV-PROMPT", "REASONING-SUMMARY", "CMD-OUTPUT", "exec_command"):
            self.assertNotIn(s, out)
        doc = self.js("--id", CX_NEW)
        self.assertEqual([e["text"] for e in self.events(doc, "thinking")], ["REASONING-SUMMARY"], "no duplicate")
        call, = self.events(doc, "tool_call")
        self.assertEqual((call["name"], call["input"]), ("exec_command", {"cmd": "ls"}))
        res, = self.events(doc, "tool_result")
        self.assertEqual((res["name"], res["output"]), ("exec_command", "CMD-OUTPUT"))
        ex, = self.events(doc, "execution")
        self.assertEqual((ex["command"], ex["output"], ex["exit_code"], ex["cwd"]),
                         ("ls", "CMD-OUTPUT", 0, str(self.proj)))
        dev = [e for e in self.events(doc, "message") if e["role"] == "developer"]
        self.assertEqual(dev[0]["text"], "DEV-PROMPT")
        self.assertEqual({e["subtype"] for e in self.events(doc, "event")}, {"task_started", "task_complete"})
        self.assertEqual(len(doc["raw"]), 16)
        self.assertEqual(doc["metadata"]["model"], "gpt-test")
        self.assertEqual(doc["metadata"]["thread"]["name"], "SQLite discussion")

    def test_codex_legacy_event_format(self):
        out = self.md("--id", CX_LEGACY)
        self.assertIn("# Legacy chat", out, "title from session_index.jsonl")
        self.assertEqual(out.count("Legacy question"), 1)
        self.assertEqual(out.count("Legacy answer"), 1)
        self.assertNotIn("ENV", out)
        doc = self.js("--id", CX_LEGACY)
        self.assertEqual(self.events(doc, "thinking")[0]["text"], "LEGACY-THINKING")
        ex, = self.events(doc, "execution")
        self.assertEqual((ex["command"], ex["output"]), ("ls", "LEGACY-OUT"))
        self.assertEqual(self.events(doc, "tool_call")[0]["name"], "local_shell")

    def test_codex_bare_2025_format(self):
        out = self.md("--id", CX_BARE)
        self.assertIn("Bare question", out)
        self.assertIn("Bare answer", out)
        self.assertNotIn("environment_context", out)
        self.assertIn(f"`{self.proj}`", out, "cwd recovered from <environment_context>")

    def test_codex_zstd(self):
        if not self.zstd_ok:
            self.skipTest("no zstd support (Python < 3.14 and no zstd CLI)")
        doc = self.js("--id", CX_ZST)
        self.assertEqual(doc["source"]["storage"], "codex-rollout-jsonl+zstd")
        self.assertIn("ZST answer", self.md("--id", CX_ZST))

    def test_codex_projection_fallback_without_zstd(self):
        def no_zstd(data, path):
            raise sr.ZstdUnavailable(f"{path} is zstd-compressed")
        with mock.patch.object(sr, "zstd_decompress", no_zstd):
            doc = self.js("--id", CX_ZST)
            out = self.md("--id", CX_ZST)
        self.assertEqual(doc["source"]["storage"], "codex-thread-history-sqlite")
        self.assertTrue(doc["notes"])
        self.assertIn("ZST question", out)
        self.assertIn("ZST answer", out)
        ex, = self.events(doc, "execution")
        self.assertEqual((ex["command"], ex["output"], ex["exit_code"]), ("/bin/zsh -lc pwd", "/w", 0))

    # ------------------------------------------------------------- OpenCode

    def test_opencode_v2(self):
        out = self.md("--id", OC_V2)
        self.assertIn("# OC v2 chat", out)
        self.assertIn("[Image 1] How to add an index?", out)
        self.assertNotIn("[attachment:", out, "inline image already mentioned in the text")
        self.assertIn("Use CREATE INDEX.", out)
        for s in ("OC-THINKING", "OC-TOOL-OUT", "SYNTHETIC-CONTINUE", "child task"):
            self.assertNotIn(s, out)
        doc = self.js("--id", OC_V2)
        self.assertEqual(self.events(doc, "thinking")[0]["text"], "OC-THINKING")
        call, = self.events(doc, "tool_call")
        res, = self.events(doc, "tool_result")
        self.assertEqual((call["name"], call["input"], res["output"]), ("shell", {"command": "ls"}, "OC-TOOL-OUT"))
        self.assertEqual({e["subtype"] for e in self.events(doc, "event")}, {"idle", "error"})
        self.assertNotIn("data", doc["events"][0]["attachments"][0], "inline file bytes stay in raw only")
        self.assertEqual(doc["raw"][0]["data"]["files"][0]["data"], "iVBORw0KGgo")
        self.assertEqual(doc["metadata"]["project"]["worktree"], str(self.proj))
        self.assertEqual(doc["metadata"]["metadata"], {"k": 1})

    def test_opencode_v1_sqlite(self):
        out = self.md("--id", OC_V1)
        self.assertIn("v1 question", out)
        self.assertIn("v1 answer", out)
        for s in ("V1-SYNTHETIC", "V1-THINKING", "V1-TOOL-ERROR"):
            self.assertNotIn(s, out)
        doc = self.js("--id", OC_V1)
        self.assertEqual(doc["source"]["storage"], "opencode-sqlite-v1")
        res, = self.events(doc, "tool_result")
        self.assertEqual((res["is_error"], res["error"]), (True, "V1-TOOL-ERROR"))
        self.assertEqual({e["subtype"] for e in self.events(doc, "event")}, {"step-start", "step-finish"})

    def test_opencode_json_storage(self):
        out = self.md("--id", OC_JSON)
        self.assertIn("json question", out)
        self.assertIn("[attachment: notes.txt (text/plain)]", out)
        self.assertIn("json answer", out)
        self.assertNotIn("JSON-TOOL-OUT", out)
        doc = self.js("--id", OC_JSON)
        self.assertEqual(doc["source"]["storage"], "opencode-json-storage")
        self.assertEqual(self.events(doc, "tool_result")[0]["output"], "JSON-TOOL-OUT")

    # ------------------------------------------------------------- Antigravity

    def test_agy_markdown(self):
        out = self.md("--agent", "agy", "--latest")
        self.assertIn("# AGY Title", out)
        self.assertIn("AGY question\n\n[image: /tmp/shot.png]", out)
        self.assertIn("AGY answer", out)
        for s in ("AGY-THINKING", "AGY-TOOL-OUT", "AGY-ERROR", "AGY-SYSTEM-MSG", "ls -la"):
            self.assertNotIn(s, out)
        self.assertIn("2026-11-18", out)

    def test_agy_json(self):
        doc = self.js("--id", AG_MAIN)
        self.assertEqual(doc["metadata"]["models"], ["Gemini Test", "gemini-test"])
        self.assertEqual(self.events(doc, "thinking")[0]["text"], "AGY-THINKING")
        call, = self.events(doc, "tool_call")
        self.assertEqual((call["name"], call["input"]), ("run_command", {"CommandLine": "ls -la"}))
        ok, err = self.events(doc, "tool_result")
        self.assertEqual((ok["name"], ok["output"], ok["call_id"]), ("run_command", "AGY-TOOL-OUT", "call_1"))
        self.assertEqual((err["is_error"], err["error"]), (True, "AGY-TOOL-ERROR"))
        subtypes = {e["subtype"]: e for e in self.events(doc, "event")}
        self.assertEqual(subtypes["error"]["text"], "AGY-ERROR")
        self.assertEqual(subtypes["system_message"]["text"], "AGY-SYSTEM-MSG")
        self.assertEqual(subtypes["step_999"]["data"], {"200": {"1": "FUTURE-STEP"}})
        self.assertEqual(len(doc["raw"]), 8)
        self.assertEqual(doc["raw"][0]["step_type_name"], "user_input")
        self.assertEqual(doc["raw"][1]["step_payload"]["20"]["3"], "AGY-THINKING")
        self.assertEqual(doc["metadata"]["summary"]["title"], "AGY Title")
        self.assertIn("gen_metadata", doc["raw_tables"])
        self.assertNotIn("raw_tables", self.js("--id", AG_MAIN, "--no-raw"))

    def test_agy_go_zero_time_and_child(self):
        doc = self.js("--id", AG_ZERO, "--no-raw")
        self.assertIsNotNone(doc["updated_at"], "falls back to step timestamps")
        self.assertIn("zero-time answer", self.md("--id", AG_ZERO))
        child = self.js("--id", AG_CHILD, "--no-raw")
        self.assertEqual((child["parent_id"], child["is_subagent"]), (AG_MAIN, True))

    # ------------------------------------------------------------- general

    def test_output_file(self):
        target = self.tmp / "out" / "t.md"
        code, out, err = self.cli("--id", CC_MAIN, "--output", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("Use SQLite", target.read_text(encoding="utf-8"))
        self.assertIn("wrote", err)
        target = self.tmp / "out" / "t.json"
        self.cli("--id", AG_MAIN, "-f", "json", "-o", str(target))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["session_id"], AG_MAIN)

    def test_sources_are_never_modified(self):
        def snapshot():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() + str(p.stat().st_mtime_ns)
                    for p in self.home.rglob("*") if p.is_file()}
        before = snapshot()
        self.cli("--list", "--all")
        for sid in (CC_MAIN, "agent-abc", CX_NEW, CX_LEGACY, CX_BARE, OC_V2, OC_V1, OC_JSON, AG_MAIN, AG_ZERO):
            self.cli("--id", sid, "-f", "json")
            self.cli("--id", sid)
        self.assertEqual(snapshot(), before)

    def test_cli_subprocess(self):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env["HOME"] = str(self.home)
        r = subprocess.run([sys.executable, str(SCRIPT), "--agent", "agy", "--latest"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("AGY answer", r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertIn("--project", r.stdout)

    def test_protobuf_and_time_helpers(self):
        self.assertEqual(sr.pb_to_json(pb((1, "file:///x"), (2, 5))), {"1": "file:///x", "2": 5})
        nested = pb((12, "file:///Users/someone/projects/an-example-workspace-path"))
        self.assertEqual(sr.pb_to_json(pb((1, nested))), {"1": {"12": "file:///Users/someone/projects/an-example-workspace-path"}})
        self.assertEqual(sr.pb_to_json(pb((3, "hi"), (3, "yo"))), {"3": ["hi", "yo"]})
        self.assertEqual(sr.pb_to_json(pb((4, "über café ✓"))), {"4": "über café ✓"})  # multi-byte UTF-8
        self.assertIn("bytes_base64", sr.pb_to_json(pb((2, b"\xff\xfe\x00")))["2"])
        self.assertEqual(sr.pb_str(pb((19, pb((2, "deep")))), 19, 2), "deep")
        self.assertIsNone(sr.to_epoch("0001-01-01 00:00:00+00:00"))
        self.assertEqual(sr.to_epoch("2026-11-18T10:00:00.123456789Z"), epoch("2026-11-18 10:00:00") + 0.123456)
        self.assertEqual(sr.to_epoch(1794999840123), 1794999840.123)
        self.assertEqual(sr.to_epoch("2026-11-18 11:25:51+00:00"), epoch("2026-11-18 11:25:51"))


if __name__ == "__main__":
    unittest.main()
