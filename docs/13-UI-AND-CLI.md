# 13 — CLI, Terminal UI, and Commands

The engine decides *what* happens; this layer decides what it *feels like*. Two agents with
identical loops feel completely different depending on whether tool calls appear as they
happen, whether diffs render inline, and whether the approval prompt shows the real command.

Everything here is stdlib only — no `rich`, no `textual`, no `prompt_toolkit`
([ADR-009](01-ARCHITECTURE.md#adr-009--stdlib-only-terminal-ui)). ANSI escapes and `termios`
are enough, and the dependency-free binary matters for a tool people install with `pipx`.

## Invocation surface

```bash
axon                                    # interactive REPL
axon -p "fix the failing test"          # headless, one shot, exit code = success
axon -p "..." --output-format json      # machine-readable, for scripts and CI
cat spec.md | axon -p "implement this"  # stdin becomes context
axon --continue                         # resume the most recent session
axon --resume                           # pick a session with arrow keys
axon --mode plan                        # explore without mutating anything
axon --model deepseek-v4-flash          # cheaper model
axon doctor                             # diagnose environment and endpoint
axon eval --all                         # run the benchmark suite
```

| Flag | Effect |
|---|---|
| `-p, --print` | One-shot; prints the final message and exits. Exit `0` if the turn ended cleanly. |
| `--output-format {text,json}` | `json` emits `{result, cost_usd, turns, usage, session_id}` |
| `--mode {default,acceptEdits,plan,bypass}` | Starting permission mode |
| `--effort {low,medium,high,xhigh,max}` | Reasoning effort; `xhigh` default for coding |
| `--allowed-tools "Read,Grep,Bash(pytest:*)"` | Session-scoped allow list |
| `--append-system-prompt TEXT` | Extra system block, appended last |
| `--workspace PATH` | Root of the path jail; defaults to cwd |
| `--no-thinking` | Disable extended thinking |
| `--dangerously-skip-permissions` | Required for `bypass`, plus an interactive confirm |

`-p` mode and the REPL share the same `Agent`. The only difference is the `on_event`
callback: the REPL passes a `Renderer`, `-p` passes a collector. That is why the loop must
never import `ui`.

---

## The rendered turn

What a turn actually looks like, and what each element is for.

```
  ● axon  claude-opus-5  xhigh   ~/projects/api   main ✓   $0.04
                                                                    ← status line
› the /users endpoint 500s when email is missing
                                                                    ← user input
  ✻ Thinking…                                                       ← collapsed by default
    The 500 suggests an unhandled exception rather than a validation
    path. I should find the handler first.

  ⏺ Grep  pattern:"def create_user"  glob:"**/*.py"                  ← tool card
    └ 1 match · src/routes/users.py:42

  ⏺ Read  src/routes/users.py  offset:30 limit:40
    └ 40 lines

  Line 47 indexes payload["email"] directly, so a missing key raises   ← streamed text
  KeyError before validation runs.

  ⏺ Edit  src/routes/users.py                                        ← awaiting approval
    │ 46      def create_user():
    │ 47  -       email = payload["email"]
    │ 47  +       email = payload.get("email")
    │ 48  +       if not email:
    │ 49  +           return jsonify(error="email is required"), 400
    │ 50      user = User(email=email)
    │
    │ Allow this edit?  [y] once   [a] always for src/**   [n] no
  › y

    └ 3 insertions, 1 deletion

  ⏺ Bash  run the users test suite
    │ pytest tests/test_users.py -q
    └ 12 passed in 0.84s

  Fixed — the missing-email case returns 400 instead of crashing.

  ─ 4 tools · 18.2k in · 1.1k out · 14.8k cached (81%) · $0.041 · 22s  ← turn footer
```

Design rules behind that layout:

- **Tool calls appear before they run,** not after. The user sees intent forming.
- **One line per call, collapsed result.** `└ 40 lines` rather than 40 lines of file. The
  agent needs the content; the human needs to know it happened.
- **The diff is the approval prompt.** Not a description of the edit — the edit.
- **`Bash` shows both the description and the real command.** The description is what the
  model claims; the command is what will run. A mismatch is visible.
- **The footer always shows cache ratio.** It is the metric that silently regresses.
- **Thinking is collapsed to two lines** with `Ctrl-T` to expand. Present, not dominant.

---

## `ui/render.py` — the renderer

```python
class Renderer:
    def on_event(self, e: StreamEvent) -> None: ...
    def tool_card(self, name: str, summary: str) -> ToolCard: ...
    def diff(self, path: Path, old: str, new: str) -> str: ...
    def markdown(self, text: str) -> str: ...
    def spinner(self, label: str) -> ContextManager: ...
    def status_line(self, state: SessionState) -> str: ...
    def turn_footer(self, result: TurnResult) -> str: ...
```

It consumes `StreamEvent` and nothing else, so it is provider-blind. It is also the **only**
module in the codebase that writes to stdout — which is what makes `-p --output-format json`
clean rather than a filtering exercise.

**Streaming markdown is the hard part.** Text arrives in fragments that split mid-token:
a fenced code block can open in one delta and close six deltas later. A stateful line-buffer
handles it — hold a partial line, emit on `\n`, track whether a fence is open, and never
attempt to style an incomplete construct.

```python
class MarkdownStream:
    def feed(self, chunk: str) -> str: ...   # returns printable output, buffers the rest
    def flush(self) -> str: ...
```
Handles: fenced code (with language-aware highlighting for the common ten), inline code,
bold/italic, headings, bullets, numbered lists. Deliberately not handled: tables, nested
blockquotes, reference links. Models rarely emit them in a terminal context and the
complexity is real.

**Diff rendering** uses `difflib.unified_diff` with a line-number gutter and 2 lines of
context. Hunks over 40 lines collapse to a summary plus the first hunk — a 400-line diff in
an approval prompt is unreadable, and unreadable prompts get approved blindly, which defeats
the point.

---

## `ui/input.py` — reading input

Three problems that a bare `input()` gets wrong:

**1. Paste bursts.** Pasting 50 lines into `input()` submits the first line and leaves the
rest as 49 queued commands. Detection: read raw with a short `select()` timeout; if more
bytes are already waiting, it is a paste — accumulate until the buffer drains, then treat the
whole thing as one submission.

**2. Multiline.** A trailing `\` continues, and an unclosed fence continues until it closes.
Continuation lines are prefixed `…` so the state is visible.

**3. History.** `readline` with a persisted history file at `~/.axon/history`, capped at 1000
entries, with a `--no-history` escape for sessions that touch secrets.

```python
def read_input(prompt: str, *, allow_multiline: bool = True) -> str: ...
```

Two prefixes are intercepted before the agent sees the line: `/` dispatches a slash command,
and `#` appends the rest of the line to the project `AGENTS.md` — the same shorthand Claude
Code uses for adding a memory mid-session.

**`Ctrl-C` semantics** are worth stating precisely, because getting them wrong corrupts the
conversation. During a tool: cancel the tool, append synthetic error results for the whole
batch, close the turn, return to the prompt ([Law 5](02-AGENT-LOOP.md#law-5--interrupts-still-close-the-turn)).
At the prompt: clear the line. Twice at an empty prompt: exit. **Never** leave a `tool_use`
unmatched — that is the bug that 400s every subsequent request.

---

## `ui/approve.py` — the approval prompt

```python
def ask_approval(tool: Tool, args: dict, decision: Decision) -> Literal["once","always","deny"]: ...
```

Three answers, one keystroke each. `always` derives the narrowest rule that would have
allowed this call — `Edit(src/**)` from an edit to `src/routes/users.py`, `Bash(pytest:*)`
from `pytest tests/ -q` — shows it, and writes it to the project config on confirmation.

Deriving the rule rather than asking the user to write one is what makes the permission
system converge: after a few sessions in a repository, the common workflow stops prompting,
and the rules that accumulated are ones the user actually approved in context.

`deny` returns a `tool_result` with `is_error: true` and the text **"User declined this
action."** — deliberately terse. A verbose refusal invites the model to argue or retry a
variation; a flat one gets acknowledgement and a change of approach.

---

## `ui/picker.py` and `ui/theme.py`

`pick(options, title) -> int | None` — raw mode via `termios`, arrow keys, `Enter`, `Esc`.
Used by `/model` and `/resume`. Restores terminal state in a `finally` block, always: a
crash that leaves the terminal in raw mode is the worst first impression a CLI can make.

`theme.py` respects `NO_COLOR`, checks `sys.stdout.isatty()`, and degrades to plain text when
piped, so `axon -p ... | jq` works without escape-sequence contamination.

---

## Slash commands

```python
@dataclass
class Command:
    name: str; help: str; run: Callable[[CommandContext, str], CommandResult]
def dispatch(line: str, ctx: CommandContext) -> CommandResult | None: ...
```

| Command | Behaviour |
|---|---|
| `/help` | Commands, current mode, active model |
| `/model [name]` | Arrow-key picker; switching provider drops thinking blocks from replayed history |
| `/mode [name]` | Injects a mid-conversation system message; does not invalidate the cache |
| `/effort [level]` | `low`…`max` |
| `/clear` | New conversation, same session file |
| `/compact [hint]` | Force rung 3; optional hint steers what the summary keeps |
| `/context` | Token budget by category (see below) |
| `/cost` | Ledger, cache ratio, uncached counterfactual |
| `/resume` | Session picker |
| `/tools` | Registry with readonly and default-permission columns |
| `/permissions` | Active rules and their source file |
| `/doctor` | Environment, endpoint probes, capability report |
| `/budget N` | Raise the session cost ceiling |
| `/todos` | Current todo state |
| `/memory` | Open the applicable `AGENTS.md` in `$EDITOR`, reload on save |
| `/init` | Explore the repo and generate `AGENTS.md` |
| `/export [path]` | Transcript to markdown |
| `/exit` | Quit |

`/context` is the one worth building carefully, because it is what makes context management
legible rather than magical:

```
/context
  claude-opus-5 · 200,000 token window

  system prompt      2,140   ▓
  tool schemas       3,890   ▓▓
  project context      710   ▓
  conversation      41,200   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
    ├ tool results  31,400     (76% of conversation)
    ├ assistant      7,900
    └ user           1,900
  ─────────────────────────────────────────────────
  total             47,940   24% of window

  largest results:  Read src/models.py      8,210
                    Bash pytest             5,470
                    Grep "def handle"       4,930

  next compaction at 170,000 (rung 3)
```

The "largest results" section is the actionable part — it tells the user which single tool
call is eating their session, and it is what makes rung-1 trimming feel like a decision
rather than an accident.

**Custom commands** load from `.axon/commands/*.md`:

```markdown
---
description: Review staged changes for bugs
argument-hint: [focus area]
allowed-tools: Read, Grep, Bash(git diff:*)
---
Review the staged changes with `git diff --cached`. Focus on $ARGUMENTS.
Report only defects you can point to a specific line for.
```

`$ARGUMENTS` and `$1`-`$9` substitute; frontmatter `allowed-tools` scopes the registry for
that invocation only.

---

## `hooks/runner.py`

Shell hooks, same contract as Claude Code's, so existing scripts largely work: JSON on stdin,
exit code as verdict.

| Event | Payload | Effect of exit 2 |
|---|---|---|
| `PreToolUse` | `{tool, args, mode}` | **Block.** stdout becomes the `tool_result`. |
| `PostToolUse` | `{tool, args, result, is_error}` | none (observation only) |
| `UserPromptSubmit` | `{prompt}` | Block the prompt; stdout replaces it |
| `SessionStart` | `{session_id, workspace}` | none; stdout injected as context |
| `SessionEnd` | `{session_id, cost, turns}` | none |

5-second timeout, environment scrubbed of the API key, never run in `bypass` mode without
being explicitly re-enabled. `PreToolUse` is the escape hatch for policy the rule syntax
cannot express — "block any edit to a file matching this repo's generated-code manifest" is a
script, not a glob.

---

## Where the surface goes next

The engine is surface-agnostic by construction, which makes a second front-end an addition
rather than a rewrite. Two tracks, both post-v1, noted so the architecture stays honest about
what it is preparing for:

**An editor surface (Cursor-class).** The same `Agent`, driven over a JSON-RPC socket instead
of a terminal, with the editor rendering diffs and approvals natively. What genuinely has to
be built: an LSP-adjacent protocol layer, inline apply/reject, and a diff-review UI. What
does *not*: the loop, the tools, permissions, context management, or the providers. The
`on_event` callback is already the seam.

**Codebase indexing.** Cursor's real differentiator is semantic retrieval over the whole
repository — embeddings plus a vector index, so "where is auth handled" is answered without
`Grep` and without reading twenty files. That is a `Search` tool alongside `Grep`, plus an
indexer that watches the filesystem. It slots in behind the existing `Tool` ABC, and it is a
substantial project of its own: chunking strategy, incremental reindexing, staleness, and
storage. Deliberately out of v1, and deliberately not designed around, because a repo-wide
index changes what the retrieval layer should look like and guessing now would be wrong.

Both depend on the same thing being true first: a loop that reliably finishes real tasks in a
terminal. That is what v1 is for.

---

That completes the design. Index: [`README.md`](../README.md#documentation).
Build order starts at [P0](07-ROADMAP.md#p0--vertical-slice-2-days).
