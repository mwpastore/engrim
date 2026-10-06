// engrim — cross-agent project memory for Pi. Written by `engrim setup --pi`.
// Re-run `engrim setup --pi` after upgrading engrim; `engrim uninstall --pi` removes it.
import { spawn } from "node:child_process"

const ENGRIM = __ENGRIM_BIN__
const HOOK = ["hook", "--agent", "pi", "--event"]
// The minder runs on every prompt, so it gets a short leash: a slow spawn costs at most this.
const PROMPT_TIMEOUT_MS = 4000
// How long the model call will wait for an in-flight minder slice before going without it.
const MINDER_WAIT_MS = 1500
// Size cap (chars) for the boot pack that rides every prompt; override in Pi's environment.
const BOOT_BUDGET = Number(process.env.ENGRIM_BOOT_BUDGET) > 0 ? Number(process.env.ENGRIM_BOOT_BUDGET) : 4000

// Node can't spawn a Windows .cmd/.bat shim (pipx/pip put one on PATH) without a shell; route those
// through cmd.exe with the path quoted, everything else runs directly.
const spawnEngrim = (args, opts) => /\.(cmd|bat)$/i.test(ENGRIM)
  ? spawn(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", `"${ENGRIM}" ${args.join(" ")}`],
          { ...opts, windowsVerbatimArguments: true })
  : spawn(ENGRIM, args, opts)

// Resolves to { ok, out }: ok is false when the process could not be spawned, timed out, or exited
// non-zero — callers must not treat that as "engrim saw this" (see flush).
function run(event, payload, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let out = ""
    let done = false
    const finish = (ok) => { if (!done) { done = true; resolve({ ok, out: out.trim() }) } }
    let p
    try {
      p = spawnEngrim([...HOOK, event], { stdio: ["pipe", "pipe", "ignore"] })
    } catch { return finish(false) }
    const timer = setTimeout(() => { try { p.kill() } catch {} finish(false) }, timeoutMs)
    p.stdout.on("data", (d) => { out += d })
    p.on("error", () => { clearTimeout(timer); finish(false) })
    p.on("close", (code) => { clearTimeout(timer); finish(code === 0) })
    p.stdin.on("error", () => {})
    p.stdin.end(JSON.stringify(payload))
  })
}
const text = async (event, payload, timeoutMs) => (await run(event, payload, timeoutMs)).out

// Same text `engrim setup --pi` appends to AGENTS.md (baked in at setup time).
const USAGE = "The block above is this session's boot pack.\n\n" + __ENGRIM_USAGE__

export default function engrimExtension(pi) {
  const boot = new Map()    // sessionID -> boot pack (built once per session, rebuilt after compaction)
  const minder = new Map()  // sessionID -> Promise<slice> for the prompt being answered
  const seen = new Map()    // sessionID -> Set of session-entry ids already logged

  const sid = (ctx) => {
    try { return ctx.sessionManager.getSessionId() } catch { return "default" }
  }

  const pack = async (id, ctx) => {
    if (!boot.has(id)) boot.set(id, await text("boot", { cwd: ctx.cwd, session_id: id, budget: BOOT_BUDGET }))
    return boot.get(id)
  }

  const messageText = (m) => {
    if (m.role === "user") {
      if (typeof m.content === "string") return m.content
      return (m.content || []).filter((p) => p.type === "text" && p.text).map((p) => p.text).join("\n")
    }
    if (m.role === "assistant") {
      // Text plus a one-line marker per tool call, so the log shows what the agent did, not just said.
      return (m.content || [])
        .filter((p) => p.type === "text" && p.text)
        .map((p) => p.text)
        .concat((m.content || []).filter((p) => p.type === "toolCall").map((p) => `[tool] ${p.name}`))
        .join("\n")
    }
    return ""
  }

  const flush = async (ctx) => {
    const id = sid(ctx)
    let branch
    try { branch = ctx.sessionManager.getBranch() } catch { return }
    const logged = seen.get(id) || new Set()
    const turns = []
    const pending = []   // ids in this batch; only promoted to `logged` once engrim has ingested them
    for (const e of branch || []) {
      if (!e || e.type !== "message" || !e.id) continue
      const m = e.message || {}
      if (m.role !== "user" && m.role !== "assistant") continue
      if (logged.has(e.id)) continue
      // An aborted/errored assistant message is incomplete; its work redoes itself, so don't log it.
      if (m.role === "assistant" && (m.stopReason === "aborted" || m.stopReason === "error")) {
        logged.add(e.id); continue
      }
      const body = messageText(m)
      if (!body) { logged.add(e.id); continue }
      pending.push(e.id)
      turns.push({ id: e.id, role: m.role, text: body, ts: e.timestamp, session_id: id })
    }
    if (turns.length) {
      // Commit the ids only after a successful ingest: if engrim couldn't be spawned or timed out,
      // the next flush retries the same turns (the log is idempotent on entry id, so no dupes).
      const r = await run("stop", { cwd: ctx.cwd, session_id: id, turns })
      if (r.ok) for (const t of pending) logged.add(t)
    }
    seen.set(id, logged)
  }

  pi.on("before_agent_start", async (event, ctx) => {
    // Fire-and-forget the minder, then give it MINDER_WAIT_MS to land: a cold Python spawn that
    // misses the window is dropped for this prompt rather than held up.
    const id = sid(ctx)
    const prompt = event.prompt || ""
    if (prompt) minder.set(id, text("prompt", { cwd: ctx.cwd, session_id: id, prompt }, PROMPT_TIMEOUT_MS))
    else minder.delete(id)
    let slice = ""
    const pending = minder.get(id)
    if (pending) {
      slice = await Promise.race([pending, new Promise((r) => setTimeout(() => r(""), MINDER_WAIT_MS))])
      minder.delete(id)
    }
    const p = await pack(id, ctx)
    if (!p && !slice) return
    // A named section keeps the boot pack self-delimiting and lets Pi diff it against the previous
    // run instead of checkpointing the whole prompt (prompt-cache friendly).
    event.systemPromptOptions.sections.engrim =
      (p ? p + "\n\n" + USAGE : "") + (slice ? (p ? "\n\n" : "") + slice : "")
  })

  // The agent loop just ended: ingest the new turns into the flight-recorder log.
  pi.on("agent_end", async (_event, ctx) => { await flush(ctx) })

  // Compaction is about to discard turns from the transcript — persist them first.
  pi.on("session_before_compact", async (_event, ctx) => { await flush(ctx) })

  // The transcript lost the turns it just summarised; the next boot pack must come from the store.
  pi.on("session_compact", async (_event, ctx) => { boot.delete(sid(ctx)) })

  // Quit / reload / session replacement: final ingest so a hard exit can't lose the session tail.
  pi.on("session_shutdown", async (_event, ctx) => { await flush(ctx) })
}
