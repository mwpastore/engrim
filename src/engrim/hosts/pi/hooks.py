"""Pi lifecycle hooks for engrim.

Pi has no shell-hook system; its extension point is a JavaScript extension loaded from
`<agent-dir>/extensions/` (default `~/.pi/agent/extensions/`). `engrim setup --pi` writes that
extension (extension.js next to this file; wiring.py installs it), and the extension shells out to
this adapter at each lifecycle moment, piping a small JSON payload on stdin and reading plain text
back on stdout:

  boot    {cwd, session_id, budget?}   -> the session-boot memory pack (injected into the system
                                          prompt once per session, and again after compaction);
                                          budget caps its size in chars (default 4000)
  prompt  {cwd, session_id, prompt}    -> the minder slice: a few records relevant to THIS prompt
  stop    {cwd, session_id, turns:[…]} -> ingests the session's new user/assistant turns into the
                                          flight-recorder log (idempotent: keyed on session-entry id)

The extension speaks the exact same payload contract as the OpenCode plugin, so the handlers are
the shared ones from the opencode host, re-exported here to keep each host self-contained:
"""
from __future__ import annotations

from engrim.hosts.opencode.hooks import handle_boot, handle_prompt, handle_stop, ingest_turns

__all__ = ["handle_boot", "handle_prompt", "handle_stop", "ingest_turns"]
