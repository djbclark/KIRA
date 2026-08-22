# Running terminus-kira locally on Apple Silicon (no Docker, no Rosetta)

This documents what it took to get `terminus-kira` (this repo's harbor
`Terminus2` agent extension) running end-to-end on an Apple Silicon Mac
using Apple's native `container` tool as the environment backend, with no
Docker Desktop, no Colima, and no Rosetta emulation — plus a follow-up
extension that lets it run against an arbitrary local task instead of only
a terminal-bench dataset.

## Summary of changes

- `terminus_kira/mounted_apple_container.py` (new): a subclass of harbor's
  `AppleContainerEnvironment` that adds host-directory bind-mount support,
  which harbor's own class doesn't have for this backend.
- `uv.lock`: harbor pinned to exactly `0.2.0` (see below for why).
- Two local wrapper scripts outside this repo, for reference:
  `~/.local/bin/terminus-kira` (dataset/benchmark runs) and
  `~/.local/bin/terminus-kira-task` (ad-hoc single-task delegation against
  a real mounted directory, no benchmark scoring).

## 1. harbor version: pin to exactly 0.2.0, not latest

`pyproject.toml` only floors `harbor>=0.1.44`. Locking to latest (0.21.0 at
the time) resolves the `apple-container` environment backend correctly
(added in harbor 0.2.0, [laude-institute/harbor#1196](https://github.com/laude-institute/harbor/pull/1196)),
but breaks this repo's own agent code: `TerminusKira._run_agent_loop`
(`terminus_kira/terminus_kira.py:897`) calls
`self._setup_episode_logging(...)`, inherited from harbor's `Terminus2`
base class. That method existed when `terminus_kira.py` was last committed
(2026-03-18) but was removed from `Terminus2` in a later harbor refactor —
upgrading to latest raises `AttributeError:
'TerminusKira' object has no attribute '_setup_episode_logging'` on every
trial, regardless of environment backend.

Fix: pin to exactly `harbor==0.2.0` (released 2026-03-25) — the earliest
PyPI release with the `apple-container` backend, and still old enough to
have `_setup_episode_logging`. `uv lock --upgrade-package harbor==0.2.0 &&
uv sync`.

If harbor needs to move off 0.2.0 in the future: either bisect where
`_setup_episode_logging` was removed and patch `terminus_kira.py` to
inline a replacement, or vendor the specific pieces of `Terminus2` this
repo actually needs.

## 2. harbor 0.2.0 CLI flags differ from what an older invocation might expect

- `--agent` is a closed enum of harbor's built-in agent names as of 0.2.0.
  A custom agent class (`terminus_kira.terminus_kira:TerminusKira`) only
  loads via `--agent-import-path` — never pass both together.
- `-i`/task-id filtering isn't a flag in 0.2.0; task filtering is
  `-t`/`--task-name` (glob-based, matches on task name within a dataset).

## 3. Native Apple Silicon (no Rosetta) needs three things

1. `container system kernel set --recommended` — without this the
   `container` VM either fails to boot or runs under an unwanted mode.
2. `container builder start` — the local BuildKit-equivalent builder
   Apple's `container` tool uses for `container build`.
3. `--force-build` on `harbor run` — without it, harbor prefers a task's
   pinned `docker_image` (terminal-bench 2.0 tasks pin **amd64-only**
   prebuilt images) over building the task's own `environment/Dockerfile`
   locally. `--force-build` makes it build the Dockerfile instead; since
   these Dockerfiles use standard multi-arch base images (official
   python/ubuntu/debian), building locally on an arm64 host produces a
   native arm64 image. No amd64 code ever runs — this sidesteps amd64
   entirely rather than emulating it.

## 4. Model access: direct provider keys hit real quota walls

Both direct-API paths failed in practice, not from a bug — from account
state:

- `ANTHROPIC_API_KEY` (sudo-secretspec managed): call succeeded
  end-to-end, but Anthropic rejected it — `"Your credit balance is too low
  to access the Anthropic API."` Account-level billing issue, unrelated to
  KIRA/harbor.
- `GEMINI_API_KEY` (sudo-secretspec managed): call succeeded, but hit a
  Google AI Studio **free-tier** quota (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`,
  `GenerateContentInputTokensPerModelPerDay-FreeTier`) within a couple of
  calls, including a **per-day** cap that no amount of retrying gets past.

Both keys are real and correctly wired — this is a billing-tier problem,
not a code problem.

### Working alternative: GitHub Copilot's monthly quota

litellm (which `terminus_kira.py:_call_llm_with_tools` calls directly, no
proxy in between — see `completion_kwargs["api_base"]`, which stays unset
here since no `--api-base` is passed anywhere in this setup) ships a
built-in `github_copilot` provider. Checked `aiuse --json`'s monthly-cadence
usage windows across every subscription on this machine; GitHub Copilot
(Individual Pro) had genuinely idle monthly quota (~71% remaining,
`aiuse` had already flagged it as likely to go unused before reset).

Model string: `github_copilot/gemini-3.1-pro-preview`. Note litellm's
bundled `model_prices_and_context_window_backup.json` only lists
`github_copilot/gemini-3-pro-preview` (no `.1`) — that catalog is stale.
Querying `https://api.githubcopilot.com/models` directly (with a token
from `litellm.llms.github_copilot.authenticator.Authenticator`) confirmed
`gemini-3.1-pro-preview` is genuinely available through Copilot; the
catalog gap only produces a harmless "model isn't mapped yet, using
fallback context limit" warning, not a failure.

**Auth**: first use requires a one-time interactive device-code OAuth —
visit `https://github.com/login/device`, enter the code printed to
stderr. This is litellm's own token cache
(`~/.config/litellm/github_copilot/access-token` +
`api-key.json`), independent of `gh auth login` or any IDE Copilot
session. The device code expires in under a minute and issuing a new
authenticator poll cycle rotates it — act on whichever code is currently
printed, not an earlier one.

Other monthly-cadence subscriptions checked and ruled out for this use
case: ClinePass and OpenCode Go both have real remaining monthly quota but
no native litellm provider and no `--api-base` CLI flag on `harbor run` to
point at a custom OpenAI-compatible endpoint — wiring either in would need
patching `terminus_kira.py`, not just a config change. Cursor's monthly
windows aren't usable at all here — no headless completions API, IDE-only.
z.ai has a native litellm provider (`zai/glm-*`) but that's a different
model family (Zhipu GLM, not Gemini), and no portable `ZAI_API_KEY` is
currently declared in sudo-secretspec — the z.ai quota visible in `aiuse`
comes from a web-session-based collector, not an API key litellm could use
directly.

## 5. Ad-hoc task delegation (not a benchmark dataset)

harbor/terminus-kira's native unit of work is a **task directory**
(`instruction.md` + `task.toml` + `environment/Dockerfile`, optionally
`tests/` for verification) — there's no "just give it an instruction
string" mode built in. `~/.local/bin/terminus-kira-task` builds one of
these on the fly per invocation (in a temp dir, cleaned up on exit) so it
can be used as "hand this agent a real task" rather than only "run it
against terminal-bench."

Two gaps that needed closing beyond what a dataset run already has:

- **No verification wanted**: `--disable-verification` skips harbor's
  test-running step entirely. `TaskPaths.is_valid(disable_verification=True)`
  doesn't require a `tests/` dir to exist, so the generated task directory
  can omit it.
- **No bind-mount support for real files**: harbor's
  `AppleContainerEnvironment.start()`
  (`harbor/environments/apple_container.py`) hardcodes exactly three bind
  mounts (agent/verifier/artifacts log dirs under `/logs`) and has no
  equivalent of the Docker backend's `--mounts-json` CLI flag. Apple's own
  `container` CLI supports arbitrary bind mounts fine
  (`container run --help` shows `-v, --volume <volume>`); harbor's Python
  wrapper for it just never passes one through.

  Fixed with `terminus_kira/mounted_apple_container.py`:
  `MountedAppleContainerEnvironment` subclasses
  `AppleContainerEnvironment`, accepts a `mount` constructor kwarg
  (`host_path:container_path`, comma-separated for multiple), and
  overrides `start()` — a full copy of the parent method (no smaller
  override seam exists; it builds and runs the container in one method) —
  to add the extra `-v` flags before invoking `container run`. Wired up
  via harbor's existing extension points: `--environment-import-path
  terminus_kira.mounted_apple_container:MountedAppleContainerEnvironment`
  plus `--ek mount=/host/path:/workspace`
  (`harbor.environments.factory.create_environment_from_config` spreads
  `--ek`/`--environment-kwarg` values as constructor kwargs — the same
  mechanism `--agent-import-path` uses for custom agents).

  Kept in sync with harbor 0.2.0's exact implementation as of this
  writing; if harbor's pin ever moves, diff
  `AppleContainerEnvironment.start()` against this file's copy before
  assuming it still applies.

Verified live: asked it to write a file with exact content into a mounted
scratch directory; the file appeared on the real host directory with
correct ownership and byte-for-byte content, no export/copy-out step
needed.

## Reference: wrapper scripts (not part of this repo)

`~/.local/bin/terminus-kira` — full dataset/benchmark runs:

```bash
terminus-kira -d 'terminal-bench@2.0'          # or -d/--path for a subset/local task
```

`~/.local/bin/terminus-kira-task` — one ad-hoc task against a real directory:

```bash
terminus-kira-task "<instruction>" [--dir HOST_DIR] [--image IMAGE]
```

Both default to `TERMINUS_KIRA_MODEL=github_copilot/gemini-3.1-pro-preview`
at `TERMINUS_KIRA_REASONING_EFFORT=high`, both run through `sudo-secretspec
run` so no credential touches the invoking shell/transcript, and both
require `container system kernel set --recommended` /
`container builder start` to have been run at least once on the host
(idempotent, `terminus-kira` runs them every invocation).
