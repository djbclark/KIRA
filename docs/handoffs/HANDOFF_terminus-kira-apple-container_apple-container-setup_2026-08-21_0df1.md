---
schema_version: 1
handoff_id: 0df1
parent_handoff_ids: []
lineage: none
chain: [terminus-kira-apple-container]
repo: KIRA
workspace: main
branch: main
head_sha: 492a5cf200f4d1cac588b67da5649d44f542ec42
created_at: 2026-08-21T21:03:47-0400
writer: claude-code
---

# Handoff — terminus-kira running natively on Apple Silicon, with ad-hoc task delegation

## The Goal

Get `terminus-kira` (this repo's harbor `Terminus2` agent extension)
running locally via harbor + Apple's native `container` tool on Apple
Silicon — no Docker/Colima, no Rosetta emulation. That was the original
ask coming into this session (continuing from a prior session's
resume-worthy blocker, see Tier 1 pointer). It expanded partway through:
once the environment was working, the operator redirected from "run it
against a terminal-bench dataset" to "I want to use it as something you
(Claude) can choose to pass work to" — i.e. ad-hoc single-task delegation
against real files, not benchmark evaluation.

## Where We Are

Fully working, verified live, changes committed and pushed to a fork.
Nothing is blocked. Two usable entry points now exist:

- `~/.local/bin/terminus-kira` — full terminal-bench dataset/benchmark
  runs (the original use case, now secondary).
- `~/.local/bin/terminus-kira-task "<instruction>" [--dir HOST_DIR]
  [--image IMAGE]` — ad-hoc single-task delegation against a real
  bind-mounted directory, no benchmark scoring. This is the now-primary
  use case per the operator's redirect.

Both default to `TERMINUS_KIRA_MODEL=github_copilot/gemini-3.1-pro-preview`
at `TERMINUS_KIRA_REASONING_EFFORT=high`, run through `sudo-secretspec
run` for credential handling, and require Apple's `container` tool to
have had `container system kernel set --recommended` +
`container builder start` run at least once (idempotent; `terminus-kira`
runs them every invocation, `terminus-kira-task` currently does not —
see Where We're Going).

KIRA repo state: `main` is 1 commit ahead of `upstream/main` (krafton-ai's
KIRA) at `492a5cf`, pushed to a new fork `djbclark/KIRA`. Working tree
clean.

Knowledge of this capability was propagated to two other places this
session, at the operator's request:
- `~/.claude/skills/terminus-kira/SKILL.md` — a new Claude Code skill (no
  existing orchestration skill fit; `herdr-orchestration` and
  `ralph-tui-orchestration` are both about peer-session/controller
  orchestration, not single sandboxed sub-agent dispatch).
- `~/.hermes/skills/autonomous-ai-agents/terminus-kira-task/SKILL.md` —
  Hermes (the separate Discord/Telegram agent bot on this machine) wrote
  this itself, invoked via `hermes --yolo -z "<briefing>"` rather than
  hand-edited, since Hermes has its own curator process that manages
  "agent-created" skills and hand-edits would fight that.

## What We Tried

1. **harbor at latest (0.21.0, via the pre-existing `~/.local/bin/terminus-kira` script's `uv lock --upgrade-package harbor` with no version pin)** — apple-container backend worked (confirmed native arm64 build, no Rosetta, via three fixes: `container system kernel set --recommended`, `container builder start`, `--force-build`), but every trial failed immediately with `AttributeError: 'TerminusKira' object has no attribute '_setup_episode_logging'`. Root cause found by reading harbor 0.2.0's installed `Terminus2` source: that method existed when `terminus_kira.py` was last committed (2026-03-18) but a later harbor refactor removed it from the base class. **Rejected** — any harbor version past whenever that refactor landed breaks this repo's own agent code, regardless of environment backend.
2. **Pin harbor to exactly 0.2.0** (`uv lock --upgrade-package harbor==0.2.0`) — earliest PyPI release (2026-03-25) with the apple-container backend, confirmed via source inspection to still have `_setup_episode_logging`. **This is what shipped.** Full trial ran end-to-end after this fix (though the *next* call — the model call — then failed on auth, see below; the harness/agent-loop layer itself was confirmed fixed at this point).
3. **harbor CLI flags from the old script assumed a different harbor era**: `-i <task>` for task filtering → doesn't exist in 0.2.0, real flag is `-t`/`--task-name` (glob). `--agent terminus_kira.terminus_kira:TerminusKira` → 0.2.0's `--agent` is a closed enum of built-in names; a custom class only loads via `--agent-import-path`, never both together. Found by reading `harbor run -h` output directly rather than guessing from memory of an older invocation.
4. **Direct `ANTHROPIC_API_KEY`** (sudo-secretspec managed, confirmed present via `sudo-secretspec check`) — request completed successfully (proved the whole pipeline: harbor→litellm→Anthropic), but Anthropic itself rejected it: `"Your credit balance is too low to access the Anthropic API."` **Rejected** — account billing issue, not something retryable or fixable in code.
5. **Direct `GEMINI_API_KEY`** (sudo-secretspec managed) at `gemini/gemini-3.1-pro-preview`, `reasoning_effort=high` — also completed the request successfully, but hit Google AI Studio free-tier quota: `RateLimitError` naming `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` and, critically, `GenerateContentInputTokensPerModelPerDay-FreeTier` — a **daily** cap. Retried once after the quoted 44s `retryDelay` and again with `--max-retries 5`; both still hit the same daily free-tier wall. **Rejected** — this key is genuinely free-tier, not a transient burst limit; would need the underlying Google Cloud project upgraded to paid billing (operator's call, not made this session) or routing through Vertex AI instead (different auth entirely — service account/ADC, not `GEMINI_API_KEY` — not attempted).
6. **FreeLLMAPI** (`github.com/tashfeenahmed/freellmapi`, researched via WebSearch at the operator's prompt) — a self-hosted proxy pooling free-tier keys across 28 providers behind one OpenAI-compatible endpoint. **Rejected as inapplicable**: it pools *free-tier API keys you already hold, one per provider* — it has nothing to plug into for ClinePass or OpenCode Go (paid subscription products with proprietary auth, not raw provider keys), and even for z.ai it would only unlock GLM models, not Gemini. Not set up this session.
7. **z.ai / GLM** (considered when operator asked "how about z.ai?") — litellm does have a native `zai/glm-*` provider, but (a) it's a different model family (Zhipu GLM, not Gemini — doesn't satisfy the operator's actual ask), and (b) no `ZAI_API_KEY` is declared in sudo-secretspec; the z.ai quota visible in `aiuse --json` comes from a web-session-based collector (like Grok's), not a portable API key. **Not attempted** — flagged as a possible future redundancy path, not pursued.
8. **`--mounts-json`** (harbor's Docker-backend flag for host bind mounts) — checked whether it also worked for apple-container. It doesn't: grepped harbor's `environments/apple_container.py` and confirmed `AppleContainerEnvironment.__init__`/`start()` never reads or applies any mount spec beyond three hardcoded log-dir mounts; `--mounts-json` is wired only into the Docker environment class in harbor's factory/CLI layer. **Rejected as-is** — led directly to building `MountedAppleContainerEnvironment` (see Key Decisions).
9. **Switching to `-e docker` for mount support** — considered as an alternative to patching harbor, since Docker's environment class *does* support `--mounts-json`. **Rejected** — this was explicitly the thing the whole session was working to avoid (operator's original ask: no Docker/Colima), and Apple's own `container` CLI already supports `-v` bind mounts natively (confirmed via `container run --help`), so the real gap was purely in harbor's Python wrapper, not a fundamental apple-container limitation.

## Key Decisions

- **Pin harbor to exactly `0.2.0`, not "latest."** Chosen over floating to latest (breaks `_setup_episode_logging`) or floating to some other pin (0.2.0 is specifically the earliest release with both apple-container support AND the still-present method — no other version satisfies both). Documented in `docs/apple-container-terminus-kira-setup.md` with an explicit note to re-diff `Terminus2._run_agent_loop` across versions before ever moving off 0.2.0.
- **Model routing: `github_copilot/gemini-3.1-pro-preview` over direct provider keys.** Chosen after systematically checking `aiuse --json`'s monthly-cadence subscription windows across every account on the machine (ClinePass, Copilot, Cursor, OpenCode Go) for genuinely idle capacity, rather than just waiting on the operator to add billing to the Anthropic/Gemini keys. Copilot was the only one with (a) real idle quota `aiuse` itself had flagged as underused, and (b) a ready-made litellm provider (`github_copilot/`) needing no new plumbing — Cursor has no headless API at all, ClinePass/OpenCode Go would need a custom `api_base` and harbor's CLI has no such flag.
- **litellm's bundled model catalog is not authoritative for what's actually available.** It only listed `github_copilot/gemini-3-pro-preview` (no `.1`), which the Copilot backend rejected outright (`"The requested model is not supported"` — a hard backend error, not a warning). Resolved by querying `https://api.githubcopilot.com/models` directly with a token from litellm's own `Authenticator` class, which showed `gemini-3.1-pro-preview` genuinely exists. Lesson recorded in the new skill file: a "model isn't mapped yet" warning is usually harmless (falls back to a generic context estimate), but an explicit backend rejection means check the live endpoint, don't trust the static catalog either way.
- **Bind-mount support: subclass harbor's environment class in KIRA's own tracked code, not patch the pip package.** Chosen over (a) monkeypatching the installed `harbor` package (not durable — wiped by `uv sync`), and (b) switching to Docker (rejected, see What We Tried #9). `terminus_kira/mounted_apple_container.py`'s `MountedAppleContainerEnvironment` is a full copy of `AppleContainerEnvironment.start()` (no smaller override seam exists — the parent builds and runs the container in one method) plus extra `-v` flags from a `mount` constructor kwarg, wired in via harbor's existing `--environment-import-path` + `--ek key=value` extension points (the same mechanism already used for the custom `TerminusKira` agent class via `--agent-import-path`). Explicit maintenance note left in the file and the docs: re-diff against harbor's real `start()` if the harbor pin ever moves off 0.2.0.
- **Ad-hoc task generation: build a throwaway `task.toml`/`instruction.md`/`Dockerfile` per invocation with `--disable-verification`, rather than extending harbor's task format.** harbor has no "just take a raw instruction string" mode; a task directory is the only unit of work it knows. Confirmed via `TaskPaths.is_valid(disable_verification=True)` that `tests/` and `solution/` are both optional when verification is off, so the generated directory only needs `instruction.md` + `task.toml` + `environment/Dockerfile`.
- **Fork rather than request upstream access.** `krafton-ai/KIRA` is the operator's employer's/collaborator's upstream, not something to push a Terminal-Bench-CLI-version compatibility patch to unilaterally. Created `djbclark/KIRA` via `gh repo fork`, repointed local `origin`→fork / `upstream`→krafton-ai (standard convention), committed and fast-forward-merged directly to fork `main` — no PR opened against upstream this session; that's a separate decision for the operator to make later if they want to contribute the fix back.
- **Propagate the capability to Hermes by talking to it, not by hand-editing its files.** Hermes has its own `curator` background process explicitly described (via `hermes curator --help`) as managing "agent-created skills" — hand-writing a file into `~/.hermes/skills/` would create an unmanaged/unprovenanced skill the curator doesn't know about. Used `hermes --yolo -z "<full briefing>"` instead so Hermes creates it through its own normal skill-authoring path.

## Evidence & Data

- Live smoke test, `chess-best-move` (terminal-bench-sample@2.0), `github_copilot/gemini-3.1-pro-preview`, `reasoning_effort=high`, apple-container: **reward = 1.0, 0 errors**, run completed in ~2m21s (`jobs/2026-08-21__20-42-36/result.json`, since cleaned up along with other scratch job dirs).
- Live smoke test of ad-hoc delegation: `terminus-kira-task "Create a file named hello.txt containing exactly the text: hello from terminus-kira" --dir /tmp/terminus-kira-smoketest` → file appeared on the real host directory, owner `djbclark:wheel`, exact byte content `hello from terminus-kira`, 24 bytes. Directory and job output cleaned up after verification.
- Anthropic failure: `litellm.BadRequestError: AnthropicException - {"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}`.
- Gemini failure: `litellm.RateLimitError` with `quotaId: "GenerateContentInputTokensPerModelPerDay-FreeTier"`, `model: "gemini-3.1-pro"`, `retryDelay: "44s"` (and `"22s"` on the retry) — the retryDelay is real but irrelevant given the *daily* dimension is what's actually exhausted.
- `sudo-secretspec check` at session start: 48 secrets found, 0 missing, 7 optional — confirms `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` were both correctly declared/present; the failures above are account-state, not secretspec wiring.
- `aiuse --json` monthly-window snapshot (2026-08-22T00:31:55Z) that drove the Copilot decision: GitHub Copilot premium requests 70.9% used / resets 2026-09-01 (flagged by `aiuse`'s own alerts as `kind: "burn"`, "82%... may go unused"); ClinePass monthly 17% used / resets 2026-09-15; Cursor included/other-models 18-44% used / resets 2026-09-02; OpenCode Go monthly 3% used / resets 2026-09-19.
- `container run --help` confirmed `-v, --volume <volume>` and `--mount type=<>,source=<>,target=<>,readonly` both exist on Apple's `container` CLI, contradicting the assumption that apple-container categorically can't do bind mounts.
- Final commit: `492a5cf` on `djbclark/KIRA` main, 3 files changed (`docs/apple-container-terminus-kira-setup.md` new, `terminus_kira/mounted_apple_container.py` new, `uv.lock` modified), 1498 insertions / 761 deletions (mostly the harbor 0.2.0 dependency tree, e.g. it pulls in `modal`/`pandas`/`pyarrow`/`opentelemetry`-related packages not present in the 0.21.0 lock).

## Operator Feedback

- Redirected mid-session from "run against a terminal-bench dataset" to "I want to use it as something you can choose to pass work to" — this is the operative framing for how this capability should be used going forward, not benchmark evaluation. Reflected in the new skill files and in which script (`terminus-kira-task`) is now the primary entry point.
- Explicit answers when asked to choose (`AskUserQuestion`): environment should **mount a real local directory** (not a fresh isolated sandbox with no host access), and completed tasks should have **no verification** (not a pass/fail tests/ dir) — both directly shaped the `MountedAppleContainerEnvironment` + `--disable-verification` design.
- Explicitly chose `github_copilot/gemini-3.1-pro-preview` over continuing to troubleshoot the Anthropic/Gemini keys, after being shown the `aiuse` monthly-window comparison.
- Asked pointed technical-accuracy questions along the way (e.g. "how is running amd64 without Rosetta possible on an M1?") that surfaced and corrected an imprecise claim in an earlier status update — the actual mechanism is avoiding amd64 entirely (build the Dockerfile locally instead of pulling the amd64-only prebuilt image), not running amd64 code without emulation. Worth stating claims about *why* something works as precisely as the claim that it works.
- Wants this documented and propagated broadly: session log, a personal orchestration skill, Hermes's own skill system, a written docs/*.md in the (forked) repo, and everything checked into git — all four executed this session per explicit instruction, not inferred.

## Where We're Going

1. **Nothing is blocking.** If picking this up cold, the single most useful next action is just *using* `terminus-kira-task` for real work — the pipeline is verified working end-to-end.
2. `terminus-kira-task` does not currently run `container system kernel set --recommended` / `container builder start` itself (unlike `terminus-kira`, which runs both every invocation). Both are idempotent, so this has caused no failures yet, but on a machine where they've never been run at all, `terminus-kira-task` would need them run manually first. Consider folding them into `terminus-kira-task` too for parity, or extracting a shared setup function both scripts call.
3. If a full `terminal-bench@2.0` benchmark run is ever wanted (the *original* pre-redirect ask — all ~90 tasks, `--n-concurrent 1`, real model spend against the Copilot monthly quota, likely hours): confirm scope with the operator first, then `~/.local/bin/terminus-kira -d terminal-bench@2.0`.
4. Not pursued this session, flagged as possible future redundancy: setting up FreeLLMAPI with the operator's own free-tier keys (multiple providers, not just Gemini) for automatic failover instead of relying solely on Copilot's monthly quota; getting a portable `ZAI_API_KEY` declared in sudo-secretspec if z.ai/GLM access is ever wanted directly.
5. No PR opened against `krafton-ai/KIRA` upstream. If the operator ever wants to contribute the harbor-0.2.0-compatibility fix back, that's a separate, later decision — not assumed here.
6. The GitHub Copilot device-code OAuth token is cached at `~/.config/litellm/github_copilot/` (access-token + api-key.json). If that cache is ever cleared, the next `terminus-kira`/`terminus-kira-task` run will print a fresh `https://github.com/login/device` code to stderr and need the one-time interactive step redone — not an error, just needs a human to act on the printed code within about a minute before it rotates.

## Quick Start

```bash
# Ad-hoc delegation (primary use case now):
cd /path/to/whatever/you/want/worked/on
terminus-kira-task "<describe the task>" --dir "$PWD"

# Check what it actually did:
ls "$PWD"   # changes land directly, no export step

# Full benchmark dataset run (secondary use case, confirm scope with operator first):
~/.local/bin/terminus-kira -d 'terminal-bench@2.0'

# Repo state:
cd ~/src/KIRA && git remote -v   # origin = djbclark/KIRA (fork), upstream = krafton-ai/KIRA
git log --oneline -3             # 492a5cf should be at or near HEAD

# Reference docs:
cat ~/src/KIRA/docs/apple-container-terminus-kira-setup.md   # full technical writeup
cat ~/.claude/skills/terminus-kira/SKILL.md                  # Claude's own decision guide
cat ~/.hermes/skills/autonomous-ai-agents/terminus-kira-task/SKILL.md   # Hermes's own copy
```
