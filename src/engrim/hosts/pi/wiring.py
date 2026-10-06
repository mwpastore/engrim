"""Pi install-time wiring: the extension file, the MCP entry, and the AGENTS.md note.

`engrim setup --pi` calls `setup()`; `engrim uninstall --pi` calls `uninstall()`.
Runtime behaviour (the boot / prompt / stop handlers extension.js shells out to) is in hooks.py
next to this file. This module needs nothing from the rest of engrim, so the CLI can import it
freely.
"""
from __future__ import annotations

import json
import os
import sys
from importlib import resources

_FILES = resources.files(__package__)

# AGENTS.md: the agent-facing instructions. `setup` appends them to <agent-dir>/AGENTS.md and
# render_extension bakes them into the extension's system-prompt block, so the two can't drift.
AGENTS_MD = (_FILES / "AGENTS.md").read_text(encoding="utf-8")

# The extension Pi loads (extension.js next to this file). `__ENGRIM_BIN__` and `__ENGRIM_USAGE__`
# are filled in by render_extension at setup time, so the extension depends on neither pi's PATH
# nor a second copy of the usage text. Plain JS + node:child_process — pi's jiti runs it as-is.
EXTENSION_JS = (_FILES / "extension.js").read_text(encoding="utf-8")


def render_extension(engrim_bin: str) -> str:
    """Bake the resolved binary into the extension source (JSON-quoted: safe for Windows paths/spaces)."""
    return (EXTENSION_JS
            .replace("__ENGRIM_BIN__", json.dumps(engrim_bin.strip('"').replace("\\", "/")))
            .replace("__ENGRIM_USAGE__", json.dumps(AGENTS_MD.rstrip(), ensure_ascii=False)))


def agent_dir(pretty: bool = False) -> str:
    """Pi's agent dir: $PI_CODING_AGENT_DIR, i.e. ~/.pi/agent by default."""
    d = os.environ.get("PI_CODING_AGENT_DIR")
    if d:
        return d
    return "~/.pi/agent" if pretty else os.path.expanduser("~/.pi/agent")


def mcp_entry(engrim_bin: str) -> dict:
    raw_bin = engrim_bin.strip('"')
    # Pi 1.x defaults MCP servers to codemode exposure (reachable only from codemode scripts);
    # the model must call engrim_* directly, so opt into direct declaration.
    return {
        "command": raw_bin,
        "args": ["serve", "--mcp"],
        "exposure": "direct",
        "description": "Project episodic memory shared across agents: recall and add curated decisions, facts, and state.",
    }


def setup(engrim_bin: str, dry_run: bool = False) -> None:
    print("Wiring Pi environment…")
    adir = agent_dir()
    ext_path = os.path.join(adir, "extensions", "engrim.js")
    mcp_path = os.path.join(adir, "mcp.json")
    agents_md = os.path.join(adir, "AGENTS.md")
    entry = mcp_entry(engrim_bin)
    if dry_run:
        print(f"[dry-run] Would write Pi extension to {ext_path}")
        print(f"[dry-run] Would register engrim MCP server in {mcp_path}")
        print(f"[dry-run] Would add usage note to {agents_md}")
        return

    # Validate the config first so a bad file can't leave a half-wired setup (extension written, MCP not).
    cfg = {}
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"{mcp_path} exists but is not valid JSON ({e}). Fix it, then re-run.")
    if not isinstance(cfg, dict):
        sys.exit(f"{mcp_path} is not a JSON object. Fix it, then re-run.")
    if "mcpServers" in cfg and not isinstance(cfg["mcpServers"], dict):
        sys.exit(f"{mcp_path}: the \"mcpServers\" key must be a JSON object. Fix it, then re-run.")

    os.makedirs(os.path.dirname(ext_path), exist_ok=True)
    tmp = ext_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_extension(engrim_bin))
    os.replace(tmp, ext_path)
    print(f"✓ wrote Pi extension {ext_path}")

    servers = cfg.setdefault("mcpServers", {})
    if servers.get("engrim") == entry:
        print(f"✓ MCP server already registered in {mcp_path}")
    else:
        servers["engrim"] = entry
        tmp = mcp_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, mcp_path)
        print(f"✓ registered MCP server in {mcp_path}")

    existing = ""
    if os.path.exists(agents_md):
        with open(agents_md, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if "engrim" in existing and "Project memory" in existing:
        print(f"✓ AGENTS.md already mentions engrim ({agents_md})")
    else:
        with open(agents_md, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n" + AGENTS_MD)
        print(f"✓ added usage note to {agents_md}")
    print("  Restart Pi (or run /reload) to load the extension and MCP server.")


def uninstall(dry_run: bool = False) -> None:
    print("Unwiring Pi environment…")
    adir = agent_dir()
    ext_path = os.path.join(adir, "extensions", "engrim.js")
    mcp_path = os.path.join(adir, "mcp.json")
    agents_md = os.path.join(adir, "AGENTS.md")
    if dry_run:
        print(f"[dry-run] Would remove {ext_path}, the engrim MCP entry, and the AGENTS.md note")
        return
    if os.path.exists(ext_path):
        os.remove(ext_path)
        print(f"✓ removed {ext_path}")
    else:
        print(f"✓ extension already removed ({ext_path})")
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            print(f"• {mcp_path} isn't strict JSON — remove the mcpServers.engrim entry by hand if present")
            cfg = None
        if isinstance(cfg, dict):
            servers = cfg.get("mcpServers")
            if isinstance(servers, dict) and "engrim" in servers:
                del servers["engrim"]
                if not servers:
                    del cfg["mcpServers"]
                if cfg:
                    tmp = mcp_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                        f.write("\n")
                    os.replace(tmp, mcp_path)
                else:
                    os.remove(mcp_path)
                print(f"✓ removed MCP entry from {mcp_path}")
    if os.path.exists(agents_md):
        with open(agents_md, encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if AGENTS_MD in existing:
            new = existing.replace("\n" + AGENTS_MD, "").replace(AGENTS_MD, "")
            with open(agents_md, "w", encoding="utf-8") as f:
                f.write(new)
            print(f"✓ removed usage note from {agents_md}")
