# How Axon creates files: the Write tool, Bash, and the tool-call loop

This explains what happens, step by step, when the model creates a file, and what changes when Bash is not available. Every claim below points at the code that does it.

## 1. The two halves: the model writes text, the tool writes the file

- **The model** decides what the file should contain and produces the text. It does this inside its response, as a structured tool call.
- **The tool** receives that text and writes it to disk. The tool does not invent content.

A tool call is structured data, not a shell command:

```json
{ "name": "Write", "arguments": { "path": "hello.py", "content": "print(\"Hello World\")\n" } }
```

The model never types a `python` or `echo` command to create the file. It emits the arguments; Axon's Python code performs the write.

## 2. How the model knows which tool to use

The tool registry (`create_default_registry`, `src/axon/tools/__init__.py:95`) builds the list of tools. Each tool publishes a `name`, a `description`, and a JSON `schema` for its arguments. That list is sent to the model with every request. The model chooses a tool from those descriptions.

For `Write`, the schema requires `path` and `content` (`src/axon/tools/fs_write.py:107-115`). The description tells the model to Read existing files before overwriting them and to prefer `Edit` for changes to existing files.

The choice is the model's, guided by descriptions. It is not a hard-coded rule. Two examples of how it works in practice:

| Request | Likely tool |
| --- | --- |
| "Create `hello.py`" | `Write` |
| "Change one line in `hello.py`" | `Edit` (`Write` would be a worse choice, since it rewrites the whole file) |

## 3. What happens inside a single tool call

When the model's response contains a tool call, `AgentLoop._run_one_inner` (`src/axon/agent/loop.py:1962`) runs these steps in order:

1. **Lookup.** `registry.get(name)` finds the tool. An unknown name becomes an error result that is sent back to the model.
2. **Hooks.** A `PreToolUse` hook may block the call or change its input.
3. **Permission check.** `PermissionEngine.check` decides allow, ask, or deny. See section 5.
4. **Approval (if needed).** In `default` mode the user is asked to approve. A denial becomes an error result.
5. **Run.** `tool.run(args, ctx)` executes. Any `ToolError` or exception becomes an error result.
6. **Result.** The result text, or the error, goes back to the model as a `tool_result` block, paired with the same `tool_use` id.

The model then reads that result and decides what to do next: finish, call another tool, or correct a mistake. This repeats until the task is done. That repeating cycle is the agent loop. A single `Write` call does not loop by itself.

## 4. What `Write` does inside `run()`

`WriteTool.run` (`src/axon/tools/fs_write.py:119`):

1. **Validate.** `path` must be non-empty. `content` must be present; an empty string is allowed and creates an empty file.
2. **Resolve the path.** `resolve_path` (`src/axon/tools/base.py:116`) strips quotes and `file://` prefixes, then passes the path through the workspace path jail. Paths outside the workspace are caught here or by the permission check.
3. **Inspect.** If the file exists, check that it is writable, then read its encoding and line endings so they can be kept. Binary or oversized files are overwritten as UTF-8.
4. **Checkpoint.** `capture_checkpoint` saves the old content before anything changes, so `/rewind` can undo the write.
5. **Write atomically.** `atomic_write_text` (`src/axon/tools/textio.py:160`) calls `atomic_write_bytes` (`textio.py:128`), which:
   - creates parent directories (`mkdir(parents=True)`),
   - writes to a temporary file in the same directory,
   - flushes and fsyncs it,
   - copies the old permission bits when overwriting,
   - renames the temporary file over the target.

   A crash mid-write leaves either the old file or the new file, never a half-written one.
6. **Record the read.** `file_state.record_read` marks the file as known. This lets a later `Edit` on the same file pass the read-before-edit check.
7. **Report.** It returns, for example, `Successfully wrote 19 characters (1 lines) to hello.py. Created file (+1).`

So when the call runs, the file is created on disk at that moment, with its directories. There is no separate "initialize the file" step. If you want an empty file, the model sends `content: ""`.

`Edit` does not create files. If the file is missing it fails with "Use Write to create new files" (`fs_write.py:31`).

## 5. Permissions: why Write asks, and what bypass changes

`PermissionEngine.check` (`src/axon/permissions/engine.py:252`) evaluates rules in this order; the first match wins:

1. Hard invariants: catastrophic shell commands and protected credential or system paths are always denied.
2. Deny rules.
3. Plan mode: anything that mutates is denied.
4. Ask rules.
5. Allow rules.
6. A tool whose `default_permission` is `deny` is denied.
7. Bypass mode: everything else is allowed.
8. Paths outside the workspace always need approval.
9. `acceptEdits` mode: file edits inside the workspace are allowed.
10. The tool's own default. `Write` and `Bash` both use `ask` (`fs_write.py:117`, `shell.py:264`).

What this means for your terminal:

- `Write  can modify · default ask` means: in normal mode, Write asks before writing.
- `mode bypass` means step 7 applies. Writes inside the workspace go through without asking. The hard invariants and deny rules still apply, and paths outside the workspace still need approval.

## 6. What happens without Bash

Bash is one tool among many. Removing it does not remove file writing.

- `Write`, `Edit`, `MultiEdit`, and `Read` use Python file operations directly and never start a process.
- `Grep` and `Glob` use ripgrep when it is installed and fall back to pure Python otherwise. They are separate tools and do not depend on Bash.
- `ToolRegistry.filtered(deny=["Bash"])` (`src/axon/tools/registry.py:233`) or a deny rule for `Bash` removes Bash from what the model can call. `Write` remains available.

Without Bash, the model can still:

- create a file (`Write`),
- change a file (`Edit`/`MultiEdit`),
- read and search files,

but it cannot:

- run `python hello.py`, tests, or a build,
- run `git`, package managers, or anything else that needs a process.

For "create the file, then run it", the model needs both tools. `Write` makes the file. `Bash` runs it. The model sees the first result before it decides on the second call.

## 7. How a Bash command is executed

`BashTool.run` (`src/axon/tools/shell.py:317`) does this:

1. **Check the command.** The permission engine first checks it against the catastrophic-command list and the credential-path list (`engine.py:262-268`).
2. **Pick the working directory.** The shell's working directory is tracked across calls. A `cd` in one call still applies to the next.
3. **Wrap the command.** On POSIX, the command is run as:

   ```sh
   <your command>
   __axon_ec=$?
   printf '\n%s%s\n' '__AXON_CWD_<token>__' "$PWD"
   exit $__axon_ec
   ```

   The sentinel line carries the final directory back to Axon, which records it (`_extract_cwd`, `shell.py:302`). The exit code is preserved.
4. **Build the argv.** `plat.shell_command_argv` (`src/axon/platform.py:1160`) returns `[shell, "-c", command]` on POSIX. PowerShell gets `-EncodedCommand`, and cmd.exe gets `chcp 65001` plus the command.
5. **Start the process.** `subprocess.Popen` runs it with `stdin` set to `DEVNULL`, stdout and stderr merged, and its own process group, so a timeout can kill the whole tree.
6. **Collect output.** Output is read in chunks, and carriage-return progress lines are collapsed. Output is capped at 30,000 characters by default.
7. **Time out.** The default is 180 seconds, with a maximum of 1800 (`shell.py:27`). Long-running processes (dev servers, watchers) can use `run_in_background`, which returns a shell id to read with `BashOutput` and stop with `KillShell`.
8. **Return.** Output and the exit code go back to the model as the tool result.

Bash does not write the model's code. It runs whatever command the model asked for. This is why it is powerful and why it asks for approval by default.

## 8. The full example: "Create hello.py and run it"

1. The model emits `Write {path: "hello.py", content: "print(\"Hello World\")\n"}`.
2. Axon checks the permission. Under `default`, the user approves. Under `bypass`, it goes through.
3. `WriteTool.run` creates `hello.py`, checkpoints first, and writes atomically. The result returns: `Created file (+1)`.
4. The model emits `Bash {command: "python hello.py", description: "Run hello.py"}`.
5. Axon checks the command, then runs it. The result returns: `Hello World`, exit code 0.
6. The model reports the outcome to you.

If step 5 fails, for example with a `SyntaxError`, the model receives the error text, can call `Edit` to fix the file, and runs Bash again. That retry is the loop in section 3.

## 9. Quick reference

| Question | Answer |
| --- | --- |
| Who writes the code? | The model, as the `content` argument. |
| Who saves it to disk? | `WriteTool.run`, through `atomic_write_text`. |
| When is the file created? | During the `Write` call, after permission approval. |
| Are parent folders created? | Yes. |
| Is there a separate init step? | No. `content: ""` creates an empty file. |
| Can `Edit` create a file? | No. Use `Write`. |
| Does Write need Bash? | No. |
| Does running the file need Bash? | Yes, or another tool that runs processes. |
| Who decides which tool? | The model, from the tool descriptions and the request. |
| What asks for permission? | Both `Write` and `Bash` ask by default. Bypass mode allows in-workspace work. |
