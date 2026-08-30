# 03 — Tools

A tool is the agent's only way to affect or observe anything. The quality of the tool
suite sets the ceiling on what the agent can do, and the quality of the tool
*descriptions* sets how often it does the right thing.

## The contract

```python
class Tool(ABC):
    name: str                      # exact, stable — the model calls it by this
    description: str               # written for the model, not for humans
    schema: dict                   # JSON Schema for input
    readonly: bool                 # gates concurrency (ADR-005)
    default_permission: Literal["allow", "ask", "deny"]

    @abstractmethod
    def run(self, args: dict, ctx: ToolContext) -> str:
        """Execute. Return output as a string for the model.
        Raise ToolError(user_message, model_message) on expected failure."""
```

`ToolContext` carries the workspace root, the `FileState` map, the shell session, the
settings, and a cancellation event. Tools receive it rather than reaching for globals,
which is what makes them unit-testable against a `tmp_path`.

### Descriptions are prompt engineering

The description is the only thing the model knows about a tool. It is not
documentation — it is instruction, and it is where most tool-use failures get fixed.
An effective description states what the tool does, when to prefer it over
alternatives, and what its non-obvious constraints are:

```python
description = """Search file contents with a regular expression.

Prefer this over Bash(grep) — it respects .gitignore, is faster, and returns
structured results.

Use `glob` to narrow the search (e.g. "**/*.py") when you know the file type.
Returns matching lines with file:line prefixes, capped at 100 matches.

To find where a symbol is DEFINED, search for the definition syntax
(e.g. "def create_user", "class User") rather than the bare name — a bare
name matches every call site and buries the definition."""
```

That last paragraph came from watching the failure mode. Descriptions are iterated
against real transcripts, not written once.

## The suite

Thirteen tools. Enough to do real work; small enough that the schema block stays cheap
and cacheable.

| Tool | readonly | Default perm | Purpose |
|------|:--------:|:------------:|---------|
| `Read` | ✅ | allow | Read a file with line numbers; records file identity |
| `Write` | ❌ | ask | Create or overwrite a file |
| `Edit` | ❌ | ask | Exact string replacement in an existing file |
| `MultiEdit` | ❌ | ask | Several edits to one file, atomically |
| `Bash` | ❌ | ask | Run a shell command in a persistent session |
| `Glob` | ✅ | allow | Find files by path pattern, newest first |
| `Grep` | ✅ | allow | Search file contents by regex |
| `Ls` | ✅ | allow | List a directory |
| `TodoWrite` | ❌ | allow | Maintain the visible task plan |
| `WebFetch` | ✅ | ask | Fetch a URL, convert to markdown, summarize |
| `Task` | ✅ | allow | Dispatch a sub-agent |
| `ExitPlanMode` | ❌ | ask | Leave plan mode with a proposed plan |
| `Doctor` | ✅ | allow | Report environment + endpoint capabilities |

### `Read`

```json
{
  "name": "Read",
  "description": "Read a file from the workspace. Returns contents with line numbers …",
  "input_schema": {
    "type": "object",
    "properties": {
      "path":   {"type": "string", "description": "Absolute or workspace-relative path"},
      "offset": {"type": "integer", "description": "1-based first line to read"},
      "limit":  {"type": "integer", "description": "Max lines (default 2000)"}
    },
    "required": ["path"],
    "additionalProperties": false
  }
}
```

Output is `cat -n` style, because line numbers are what make `Edit` and error messages
addressable:

```
     1→from flask import Flask, jsonify, request
     2→
     3→app = Flask(__name__)
```

Behaviour worth specifying:

- Files over 2,000 lines are truncated with an explicit note naming the total, so the
  model knows to page with `offset` rather than assuming it saw everything.
- Individual lines are truncated at 2,000 characters (minified bundles otherwise
  consume the whole window).
- Binary files are refused with their type and size, not dumped as mojibake.
- **Every successful read records `(mtime_ns, sha256)` into `ctx.file_state`.** This is
  the read half of the invariant below.

### `Edit`

```json
{
  "name": "Edit",
  "input_schema": {
    "type": "object",
    "properties": {
      "path":        {"type": "string"},
      "old_string":  {"type": "string", "description": "Exact text to replace, including indentation"},
      "new_string":  {"type": "string"},
      "replace_all": {"type": "boolean", "default": false}
    },
    "required": ["path", "old_string", "new_string"],
    "additionalProperties": false
  }
}
```

Exact string replacement rather than line numbers or diffs, for a specific reason:
**it fails loudly when the model's mental model is wrong.** Line-number edits apply
successfully to the wrong line when the file has shifted. A unified diff can be applied
fuzzily. An exact-match requirement that errors on zero matches, and errors on multiple
matches unless `replace_all` is set, turns "the model was confused" into a clean
recoverable error instead of silent corruption.

Preconditions, checked in order:

1. Path resolves inside the workspace jail ([`06-SECURITY.md`](06-SECURITY.md#the-path-jail)).
2. File exists and has been read this session.
3. File is unchanged since that read.
4. `old_string` occurs exactly once, or `replace_all` is true.
5. `old_string != new_string`.

Writes are atomic: write to `path.axon-tmp`, `fsync`, then `os.replace`. A crash
mid-edit leaves the original intact.

### The read-before-edit invariant

The correctness mechanism the whole tool suite is built around.

```python
@dataclass(frozen=True)
class FileRecord:
    mtime_ns: int
    sha256:   str
    read_at_turn: int

class FileState:
    def record(self, path: Path, turn: int) -> None: ...

    def check_writable(self, path: Path) -> None:
        if not path.exists():
            return                                    # creation is always fine
        rec = self._records.get(path.resolve())
        if rec is None:
            raise ToolError(
                model_message=(
                    f"You have not read {path} in this session. Read it first — "
                    f"editing a file you have not seen risks corrupting it."
                )
            )
        if path.stat().st_mtime_ns != rec.mtime_ns and _sha256(path) != rec.sha256:
            raise ToolError(
                model_message=(
                    f"{path} has changed since you read it (another process, or your "
                    f"own earlier edit). Re-read it before editing."
                )
            )
```

Why both `mtime` and `sha256`: `mtime` alone gives false positives (a `touch`, a
checkout that rewrites an identical file, coarse filesystem timestamps), which would
annoy the model into pointless re-reads. Hashing alone is slow on large files. Check
`mtime` first as a cheap gate; only hash when it differs. False positives cost one
re-read, false negatives cost data — so the check is deliberately conservative.

This invariant is also what makes multi-agent and multi-window use safe. If you edit a
file in your editor while Axon is working, its next edit to that file is rejected with
an actionable message rather than clobbering your change.

### `Bash`

The most powerful and most dangerous tool.

```json
{
  "name": "Bash",
  "input_schema": {
    "type": "object",
    "properties": {
      "command":     {"type": "string"},
      "description": {"type": "string", "description": "5-10 words, shown to the user in the approval prompt"},
      "timeout_ms":  {"type": "integer", "default": 120000}
    },
    "required": ["command", "description"],
    "additionalProperties": false
  }
}
```

Design points:

- **Persistent session.** One long-lived `bash` subprocess per Axon session, driven with
  a sentinel-delimited protocol. `cd`, `export`, and activated virtualenvs persist
  across calls, which matches what the model expects and avoids `cd x && cd x && …`
  accumulating in every command.
- **Sentinel protocol.** Write `command; printf "\n__AXON_%s_%d__\n" "$RANDOM_ID" "$?"`,
  then read until the sentinel. That yields the exit code without a second round trip
  and cannot be spoofed by ordinary command output.
- **Output cap** at 30,000 characters, truncated in the middle with a marker. The head
  carries the command's intent; the tail carries the error. The middle of a 200,000-line
  build log carries neither and would evict the conversation from the context window.
- **Timeout** kills the process group, not just the shell, so a hung `pytest` does not
  leave orphans.
- **Environment scrubbing.** `AXON_API_KEY` is removed from the subprocess environment.
  A command that prints `env` should not leak the key into the transcript.
- **`description` is required** because it is what the human sees in the approval
  prompt. Requiring the model to state its intent in five words makes approval decisions
  fast and makes bad intent visible.

The `description` field is a small idea with a large effect: `⏺ Bash — run the user
test suite` reads instantly, where a 200-character `pytest` invocation does not.

### `Grep` and `Glob`

`Grep` shells out to `ripgrep` when it is on `PATH` — correct `.gitignore` handling,
fast, well-tested. Without `rg` it falls back to a pure-Python walker with the same
output format, so behaviour degrades in speed rather than in capability. Results are
capped at 100 matches with a count of what was elided.

`Glob` uses `pathlib.Path.rglob`, filters through `.gitignore`, and **sorts by mtime
descending**. That ordering is deliberate: in a task about recent work, the relevant
files are the recently touched ones, and the model reads from the top of a list.

### `TodoWrite`

Looks trivial, matters more than it looks.

```json
{
  "todos": [
    {"content": "Reproduce the failure", "status": "completed",   "activeForm": "Reproducing the failure"},
    {"content": "Fix the validation",    "status": "in_progress", "activeForm": "Fixing the validation"},
    {"content": "Add a regression test", "status": "pending",     "activeForm": "Adding a regression test"}
  ]
}
```

Two functions. For the user, it is a progress display during a long task. For the
model, it is externalized working memory that survives compaction — the plan is
re-stated in the tool result on every update, so the goal does not drift over forty
turns. Exactly one item may be `in_progress`; the tool rejects more, which forces
sequencing rather than a vague "working on everything."

### `WebFetch`

Fetch → strip to markdown → **summarize with a cheap model against the caller's
question**, and return the summary rather than the page. A 50,000-token documentation
page returned raw would evict the working context; `deepseek-v4-flash` reducing it to
800 relevant tokens costs a fraction of a cent. Redirects to a different host are
returned as a message rather than followed, and responses are cached for 15 minutes.

### `Task`

```json
{
  "prompt":       {"type": "string", "description": "A complete, self-contained instruction — the sub-agent sees none of this conversation"},
  "description":  {"type": "string", "description": "3-5 words for the progress display"},
  "tool_filter":  {"type": "array",  "description": "Tool names to allow; defaults to read-only tools"}
}
```

The description carries the crucial warning that the prompt must be self-contained.
Sub-agents are context-isolated, and the most common failure is a prompt like *"check
the other one too"* that means nothing without the parent conversation.

## Schema conventions

Applied to all thirteen:

- `additionalProperties: false` and an explicit `required` list. With `strict: true` on
  the Anthropic path this makes the model's arguments structurally guaranteed rather
  than hopefully-correct.
- Descriptions on every property. Field names alone are under-specified — `path`
  relative to what, `offset` from zero or one?
- Flat objects. Nested schemas raise the malformed-argument rate for no benefit at this
  scale.
- Small enums over free strings where the value space is closed.

## Output discipline

Tool output is the dominant consumer of context in a coding agent — usually more than
the model's own output and the user's prompts combined. Three rules:

1. **Cap everything.** Every tool has a byte ceiling and truncates with a marker that
   states what was elided and how to get more.
2. **Truncate in the middle**, keeping head and tail. Signal lives at both ends.
3. **Return structure, not prose.** `src/routes/users.py:47: email = payload["email"]`
   is more useful per token than a sentence describing it.

## Anthropic-defined tools: considered, rejected

The API offers server-defined `bash_20250124` and `text_editor_20250728` tools that need
no schema and are more heavily trained. They were considered and rejected:

- They do not exist on the OpenAI-compatible path, which would fork the tool suite by
  provider.
- Their behaviour is fixed — no `readonly` flag for concurrency, no permission hooks, no
  file-state tracking, no output caps.
- They may not pass through the proxy.

Custom tools that work identically on all four models are worth the schema cost.

---

Next: [`04-PROVIDERS.md`](04-PROVIDERS.md) — talking to two protocols at once.
