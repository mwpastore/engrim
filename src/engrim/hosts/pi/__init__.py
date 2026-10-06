"""Everything engrim needs for the Pi coding agent, in one place:

  hooks.py    runtime — the boot / prompt / stop handlers `engrim hook --agent pi` runs
  wiring.py   install time — `engrim setup|uninstall --pi`: extension file, MCP entry, AGENTS.md
  extension.js  the extension setup writes into <agent-dir>/extensions/ (placeholders filled by setup)
  AGENTS.md   the agent-facing usage note, appended to <agent-dir>/AGENTS.md and baked into the extension

Kept empty so importing `.wiring` never pulls in `.hooks` (which imports the CLI).
"""
