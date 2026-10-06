"""Pi integration: the adapter events, the extension file, and setup/uninstall wiring."""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from engrim.cli import connect, main, add_memory
from engrim.hosts.pi import hooks as pi
from engrim.hosts.pi import wiring as host


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    return home


# ------------------------------------------------------------------ adapter events

def test_boot_returns_pack_for_reported_cwd(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    add_memory(c, project=proj, type="decision", summary="Chose SQLite over Postgres", tags=["db"], origin_agent="pi")
    c.close()

    text = pi.handle_boot({"cwd": proj, "session_id": "s1"}, db_path=db, print_output=False)
    assert "memory restored" in text
    assert "Chose SQLite over Postgres" in text
    assert "via Pi" in text


def test_boot_honours_payload_budget(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    for i in range(40):
        add_memory(c, project=proj, type="fact", summary=f"fact number {i} " + "x" * 80)
    c.close()
    full = pi.handle_boot({"cwd": proj}, db_path=db, print_output=False)
    small = pi.handle_boot({"cwd": proj, "budget": 600}, db_path=db, print_output=False)
    junk = pi.handle_boot({"cwd": proj, "budget": "lots"}, db_path=db, print_output=False)
    assert len(small) < len(full)
    assert "fact number" in small
    assert junk == full                      # a bad budget falls back to the default, never errors


def test_boot_empty_project_is_quiet(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "empty")
    os.makedirs(proj)
    text = pi.handle_boot({"cwd": proj}, db_path=db, print_output=False)
    assert text.startswith("(no memory for project=")


def test_prompt_returns_relevant_slice_or_nothing(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    add_memory(c, project=proj, type="fact", summary="Deploy pipeline uses GitHub Actions with a matrix build",
               detail="ubuntu + windows runners")
    c.close()

    hit = pi.handle_prompt({"cwd": proj, "prompt": "why does the deploy pipeline matrix fail on windows"},
                           db_path=db, print_output=False)
    assert "Possibly-relevant project memory" in hit
    assert "GitHub Actions" in hit

    miss = pi.handle_prompt({"cwd": proj, "prompt": "hi"}, db_path=db, print_output=False)
    assert miss == ""


def test_stop_ingests_turns_idempotently(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    turns = [
        {"id": "ent_u1", "role": "user", "text": "let's use uv instead of pip",
         "ts": "2025-09-11T12:00:00+00:00"},
        {"id": "ent_a1", "role": "assistant", "text": "Decision: switch to uv.\n[tool] bash: uv init",
         "ts": "2025-09-11T12:00:05+00:00"},
        {"id": "ent_x1", "role": "assistant", "text": "", "ts": "2025-09-11T12:00:06+00:00"},   # empty: skipped
    ]
    payload = {"cwd": proj, "session_id": "pi_s1", "turns": turns}
    assert pi.handle_stop(payload, db_path=db, print_output=True) == 2
    assert json.loads(capsys.readouterr().out.strip()) == {"logged": 2, "ok": True}
    # resend the same session: nothing new is written
    assert pi.handle_stop(payload, db_path=db, print_output=False) == 0

    c = connect(db)
    rows = c.execute("SELECT session, role, content, msg_uuid, ts FROM log WHERE project=? ORDER BY id",
                     (proj,)).fetchall()
    assert [(r["role"], r["msg_uuid"], r["session"]) for r in rows] == [
        ("user", "ent_u1", "pi_s1"), ("assistant", "ent_a1", "pi_s1")]
    assert rows[0]["ts"].startswith("2025-09-11")          # ISO string passed through
    assert "[tool] bash" in rows[1]["content"]


def test_hook_cli_dispatch(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "cwd": proj, "session_id": "s", "turns": [{"id": "m1", "role": "user", "text": "hello world"}]})))
    main(["--db", db, "hook", "--agent", "pi", "--event", "stop"])
    assert json.loads(capsys.readouterr().out.strip()) == {"logged": 1, "ok": True}

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": proj})))
    main(["--db", db, "hook", "--agent", "pi", "--event", "boot"])
    assert "no memory for project" in capsys.readouterr().out


# ------------------------------------------------------------------ extension + setup

def test_render_extension_bakes_quoted_binary():
    src = host.render_extension('"C:\\Users\\tim\\pi\\engrim.EXE"')
    assert 'const ENGRIM = "C:/Users/tim/pi/engrim.EXE"' in src
    assert "__ENGRIM_BIN__" not in src
    for hook in ("before_agent_start", "agent_end", "session_before_compact",
                 "session_compact", "session_shutdown"):
        assert hook in src
    assert '["hook", "--agent", "pi", "--event"]' in src


def test_setup_pi_dry_run(fake_home, tmp_path, capsys):
    main(["--db", str(tmp_path / "m.db"), "setup", "--pi", "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run] Would write Pi extension" in out
    assert "[dry-run] Would register engrim MCP server" in out
    assert not (fake_home / ".pi").exists()


def test_setup_pi_merges_config_and_is_idempotent(fake_home, tmp_path, capsys):
    adir = fake_home / ".pi" / "agent"
    adir.mkdir(parents=True)
    (adir / "mcp.json").write_text(json.dumps({
        "mcpServers": {"other": {"command": "npx", "args": ["-y", "other-mcp"]}},
    }), encoding="utf-8")
    (adir / "AGENTS.md").write_text("# my rules\n", encoding="utf-8")

    main(["--db", str(tmp_path / "m.db"), "setup", "--pi"])
    out = capsys.readouterr().out
    assert "✓ wrote Pi extension" in out
    assert "✓ registered MCP server" in out
    assert "✓ added usage note" in out

    cfg = json.loads((adir / "mcp.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["other"]["command"] == "npx"       # untouched
    engrim = cfg["mcpServers"]["engrim"]
    assert engrim["args"] == ["serve", "--mcp"]
    assert engrim["exposure"] == "direct"   # pi defaults MCP servers to codemode; the model must call engrim_* directly

    ext = adir / "extensions" / "engrim.js"
    assert ext.exists()
    assert "export default function engrimExtension" in ext.read_text(encoding="utf-8")

    agents = (adir / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# my rules\n")
    assert "engrim_recall" in agents

    # second run: no duplicates, no churn
    main(["--db", str(tmp_path / "m.db"), "setup", "--pi"])
    out = capsys.readouterr().out
    assert "✓ MCP server already registered" in out
    assert "✓ AGENTS.md already mentions engrim" in out
    assert (adir / "AGENTS.md").read_text(encoding="utf-8").count("engrim_recall") == 1


def test_setup_autodetects_pi(fake_home, tmp_path, capsys):
    (fake_home / ".pi" / "agent").mkdir(parents=True)
    main(["--db", str(tmp_path / "m.db"), "setup", "--dry-run"])
    out = capsys.readouterr().out
    assert "Pi (~/.pi/agent)" in out
    assert "[dry-run] Would write Pi extension" in out


def test_setup_respects_pi_coding_agent_dir(fake_home, tmp_path, capsys, monkeypatch):
    adir = tmp_path / "pi-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(adir))
    main(["--db", str(tmp_path / "m.db"), "setup", "--pi"])
    assert (adir / "extensions" / "engrim.js").exists()
    assert not (fake_home / ".pi").exists()


def test_uninstall_pi_reverses_setup(fake_home, tmp_path, capsys):
    adir = fake_home / ".pi" / "agent"
    adir.mkdir(parents=True)
    (adir / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}), encoding="utf-8")
    (adir / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
    db = str(tmp_path / "m.db")
    main(["--db", db, "setup", "--pi"])
    main(["--db", db, "uninstall", "--pi"])
    out = capsys.readouterr().out
    assert "✓ removed" in out
    assert not (adir / "extensions" / "engrim.js").exists()
    cfg = json.loads((adir / "mcp.json").read_text(encoding="utf-8"))
    assert cfg == {"mcpServers": {"other": {"command": "npx"}}}
    assert (adir / "AGENTS.md").read_text(encoding="utf-8") == "# mine\n"


def test_setup_pi_rejects_non_object_mcpservers_before_writing_extension(fake_home, tmp_path, capsys):
    adir = fake_home / ".pi" / "agent"
    adir.mkdir(parents=True)
    (adir / "mcp.json").write_text('{"mcpServers": "nope"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "m.db"), "setup", "--pi"])
    assert not (adir / "extensions" / "engrim.js").exists()   # nothing half-wired


def test_pi_is_a_valid_origin_agent(tmp_path):
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "-p", "/p", "-t", "decision", "-s", "from pi", "--origin-agent", "pi"])
    c = connect(db)
    assert c.execute("SELECT origin_agent FROM memories").fetchone()[0] == "pi"


# ------------------------------------------------------------------ extension semantics (node)

_HARNESS = r"""
import fs from "node:fs"
import engrimExtension from "./engrim.js"
const dir = new URL(".", import.meta.url).pathname
const handlers = {}
engrimExtension({ on: (name, fn) => { handlers[name] = fn } })
const entries = [
  { type: "message", id: "u1", timestamp: "2025-09-11T12:00:00Z",
    message: { role: "user", content: "hello" } },
  { type: "message", id: "a1", timestamp: "2025-09-11T12:00:01Z",
    message: { role: "assistant", stopReason: "stop",
               content: [{ type: "text", text: "hi" }, { type: "toolCall", name: "bash" }] } },
  { type: "message", id: "x1", timestamp: "2025-09-11T12:00:02Z",
    message: { role: "assistant", stopReason: "aborted", content: [{ type: "text", text: "partial" }] } },
  { type: "message", id: "t1", timestamp: "2025-09-11T12:00:03Z",
    message: { role: "toolResult", toolName: "bash", content: [{ type: "text", text: "out" }], isError: false } },
]
const ctx = { cwd: "/repo", sessionManager: { getSessionId: () => "S", getBranch: () => entries } }
const stopFile = dir + "last-stop.json"
const take = () => { const t = JSON.parse(fs.readFileSync(stopFile, "utf8")); fs.unlinkSync(stopFile); return t }
fs.writeFileSync(dir + "FAIL_STOP", "")
await handlers.agent_end({}, ctx); const first = take().turns.map(t => t.id)   // ingest failed: nothing is marked seen
fs.unlinkSync(dir + "FAIL_STOP")
await handlers.agent_end({}, ctx)                                              // aborted skipped, tool line rendered
const secondPayload = take()
const second = secondPayload.turns.map(t => t.id)
const toolLine = secondPayload.turns.find(t => t.id === "a1").text
await handlers.agent_end({}, ctx); const third = fs.existsSync(stopFile)       // and after success nothing is resent
const sys1 = { prompt: "do x", systemPrompt: "BASE", systemPromptOptions: { sections: {} } }
await handlers.before_agent_start(sys1, ctx)
const packOk = typeof sys1.systemPromptOptions.sections.engrim === "string"
  && sys1.systemPromptOptions.sections.engrim.startsWith("PACK")
  && sys1.systemPromptOptions.sections.engrim.includes("engrim_recall")
fs.unlinkSync(dir + "last-boot.json")
const sys2 = { prompt: "do y", systemPrompt: "BASE", systemPromptOptions: { sections: {} } }
await handlers.before_agent_start(sys2, ctx)
const cachedBoot = !fs.existsSync(dir + "last-boot.json")           // second call served from cache: no spawn
await handlers.session_compact({}, ctx)
const sys3 = { prompt: "do z", systemPrompt: "BASE", systemPromptOptions: { sections: {} } }
await handlers.before_agent_start(sys3, ctx)
const rebootAfterCompact = fs.existsSync(dir + "last-boot.json")    // compaction invalidates the cache: re-spawned
console.log(JSON.stringify({ first, second, toolLine, third, packOk, cachedBoot, rebootAfterCompact }))
"""

_FAKE_ENGRIM = """#!/bin/sh
ev="$5"; cat > "$(dirname "$0")/last-$ev.json"
if [ "$ev" = stop ] && [ -e "$(dirname "$0")/FAIL_STOP" ]; then exit 1; fi
[ "$ev" = boot ] && echo PACK
[ "$ev" = stop ] && echo '{"logged":1,"ok":true}'
exit 0
"""


# NOTE: this skips on Windows, so the cmd.exe shim branch in spawnEngrim has string-presence
# coverage only. Verify manually there: `engrim setup --pi` with a pipx-installed engrim.cmd,
# then confirm the boot pack appears in a session.
@pytest.mark.skipif(shutil.which("node") is None or sys.platform == "win32",
                    reason="needs node and a POSIX shell")
def test_extension_flush_skips_aborted_and_retries_failed_ingest(tmp_path):
    """Drive the real extension under node with a fake engrim: aborted assistant turns are never
    sent, a failed stop-ingest leaves the turns pending for the next flush, and a successful one
    retires them; the boot pack is cached per session and rebuilt after compaction."""
    fake = tmp_path / "fake-engrim.sh"
    fake.write_text(_FAKE_ENGRIM, encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "engrim.js").write_text(host.render_extension(str(fake)), encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "first": ["u1", "a1"], "second": ["u1", "a1"], "toolLine": "hi\n[tool] bash",
        "third": False, "packOk": True, "cachedBoot": True, "rebootAfterCompact": True}
