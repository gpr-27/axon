# The Axon app (browser)

> **Status: under active development.** The app is not finished. Pages, layout and features change between
> commits, and some parts may be incomplete or rough. The terminal is the supported way to use Axon; treat
> the app as an extra view onto the same data.

The app is a local web page served by your own Axon process. It shows and drives the same sessions, files,
settings and usage data that the terminal uses.

## Starting it

```bash
axon open                       # start a server, print the link, open your browser
axon open --no-open             # print the link only
axon open --port 9000           # default 8787; a free port is used if it is busy; 0 picks a random one
axon open --workspace DIR       # the project to show (default: the current directory)
axon dash                       # same thing under its older name
```

Inside a session, `/open` does the same (aliases `/dash`, `/web`); `/open stop` stops it. A page opened from
`/open` is attached to that running session, so replies you send from the page are queued onto it rather than
written by a second process. `axon open` on its own starts a separate server that is not tied to a terminal
session; `Ctrl+C` stops it. The browser is not opened over SSH, in CI, on a Linux machine with no display, or
when `AXON_NO_BROWSER` is set; the link is printed instead.

## What it contains

The sections, as named in the page: Chat, Files, Shell, Terminal, Commands, Insights, Sessions, Usage, Memory,
Extensions and Settings. Chat sends messages to a session and shows its transcript. Files browses and edits files
in the project. Shell runs single commands line by line (no full-screen programs). Commands, Insights, Usage,
Sessions, Memory, Extensions and Settings give browser views of the matching slash commands, usage data, saved
sessions, memory files, installed extensions and settings. Secret-looking settings are masked.

Extensions has tabs for MCP servers, skills, plugins, hooks, agents and slash commands, plus a Marketplace tab. Each
button runs the same slash command as in the terminal (`/mcp approve`, `/plugin update`, `/marketplace install`...)
and shows its output under the list; destructive ones ask first. Chats started in the app connect your MCP servers.
Servers from a project's `.mcp.json` wait for Approve. Hooks, plugins and `.axon/mcp.json` servers that a folder
brings stay off until you press "Trust this folder" on that page, after reading the list of what would run. An MCP
server that asks for input shows a form in the chat. `@file`, `@agent-name` and `@server:uri` work in the composer.

## First run, starting points and recent projects

- **Welcome.** When no model works yet, the app opens a short guide: choose an online service or a local model, paste
  the key, press Test, then Use this provider, and pick the project folder. It reuses the Settings > Providers form.
  Skip it and it does not come back; if a model already works it never appears. Reopen it from **More > Welcome and
  setup guide** or `Cmd+K`.
- **Empty chat.** Instead of instructions, a new chat offers a few starter prompts taken from the folder: "Explain this
  project", "Find bugs", "Write tests for" a real source file that has no test yet, "What changed recently?" when git
  shows changes, and "Fix the failing tests" after a failed test run. Clicking one fills the message box; nothing is sent.
- **"?" notes.** Progress sections, the mode picker and the main pages carry a small "?" that explains the word (context,
  tokens, cache, modes) in a sentence or two. Esc closes it.
- **Project overview.** The top of **Insights** shows files and languages, the git branch and last commits, the detected
  test command and its last result (from `/test` or a test command Axon ran in a chat), spend in the last 7 days, the
  last chats and memory notes, each linking to where it lives. The terminal has the same facts in `/overview`.
- **Recent projects.** The project name in the top bar opens a menu of projects that have saved chats, newest first. Click
  one to switch. A folder that no longer exists is greyed out with a Remove button (the chats are kept). `Cmd+K` also
  lists them. This page cannot switch folders when it is attached to a running terminal session.

## Scheduled tasks and notifications

- **Tasks.** More > Tasks (or `Cmd+K > Open Tasks`) runs a prompt in a folder daily, on chosen weekdays, every so often
  (at least a minute) or once. Add, edit, pause, delete and Run now; History lists each run with status, time and cost,
  and Open shows the run as a normal chat. In the terminal the same tasks are `/schedule` (menu, or `/schedule add daily
  09:00 <prompt>`, `list`, `run`, `pause`, `resume`, `history`, `delete`). They live in `AXON_HOME/schedule/`.
- **Tasks run only while Axon is open** (this app or a terminal window). There is no system service or login item, which
  would change your computer. A run that was due while Axon was closed is recorded as missed and not replayed; a task can
  ask for one catch-up run. A task never runs twice at once, in this window or another one.
- **Safe by default.** A task cannot ask questions, so anything needing approval is refused. It starts read-only; "can edit
  files" is a choice; "full access" (no checks at all) needs an explicit confirmation and is never the default. Every run has
  a spending limit (default $1.00) and a 30 minute time limit.
- **Notifications** (Settings > Preferences > Notifications). Off until you switch on browser notifications, which is the
  only moment the browser asks for permission. Choose how long a turn must take before it notifies (default 30 s; permission
  requests and questions always notify), whether the tab title flashes while the tab is in the background, and whether
  finished tasks notify. Messages say only that something happened, never file contents or answers. The terminal has its
  own: `/notify` (when, and how long a turn must run) and `/notify terminal off|bell|osc9|osc777`, off by default.

## Comparing models, cost estimates and budgets

- **Compare models.** Chat options > Compare models sends one prompt to two to four models at once, in side-by-side
  columns that stream together, each with its time, tokens and cost and its own Stop. It is read-only: every model can read
  files and search the project and nothing else, so no file changes and no command runs. "Continue with this one" forks the
  chat with that model and its answer. Every call is logged in Usage as source `compare`, so budgets count it. In the
  terminal: `/compare <model> <model> [<model>...] <prompt>` (put `--` before the prompt if a word of it looks like a model
  name); the answers print as each one finishes. A model with no known price shows "no price known" and is not counted.
- **Cost estimate.** When the next turn may cost more than a threshold (Settings > Cost estimate above, default $0.25, 0 turns
  it off), a small line under the message box shows a range and what it is based on: the context the request really
  carries, the model's price, and the calls and output of the last turns in that chat. It is an estimate, it never blocks
  sending, and it shows nothing for a model whose price is unknown.
- **Budget notices.** A quiet notice appears once each at 50%, 80% and 100% of the session, daily or monthly budget (in the
  chat and in the terminal). The session budget is the smaller of the hard ceiling (`/budget 5`) and the per-session guard.
- **Daily and monthly budgets.** Usage > Daily and monthly budget, or `/budget day 5` and `/budget month 50`, limit a project
  by what the usage log says was spent today or this month. Once used up, new turns do not start until you raise it.
- **Spending.** Today, this week (from Monday) and this month show in Progress > Usage and in the `/usage` card and screen,
  for this project (and all projects in the card).

## Messages, find, bookmarks and export

- **On a message.** Hover (or Tab to) one of your messages for Copy and Edit and resend; on Axon's reply for Copy, Regenerate (also
  with another model), Bookmark and Branch from here. Edit and resend and Regenerate go back to just before your message: a
  dialog says how many messages Axon forgets, and, when Axon edited files after that point, offers to put them back from the
  checkpoints (files you changed yourself since are listed and left alone unless you allow overwriting). The removed messages
  stay in the saved session file. Branch from here opens a new chat with the conversation up to that reply; the original is
  unchanged. Not offered while Axon is working, for a chat open in a terminal, or for a message with an attachment.
- **Find in the chat.** `Ctrl/Cmd+F` in the chat: next and previous, highlighted matches, and text inside collapsed tool rows
  (the row opens). Messages that are not loaded yet are counted, with a button that loads them.
- **Bookmarks.** Bookmark a reply; Chat options > Bookmarks lists them and jumps to the message. They are kept in
  `bookmarks.json` beside the session. In the terminal: `/bookmark [note]` and `/bookmarks [show|remove <n>]`.
- **Export or share** (Chat options, Sessions, `/export`): Markdown, a one-file web page (inline style, no scripts), PDF (the
  browser's print dialog, with a print layout) or JSON; tool details can be left out. Secrets are removed with Axon's redactor,
  the dialog says how many, and shows a preview before you save. `/export html|pdf` and `--no-tools` do the same in the terminal.

## Saved prompts and the checkpoint timeline

- **Saved prompts.** Prompts you reuse, with fill-in fields written `{{file}}` or `{{language}}`. Open them from the `+` menu
  under the message box (Saved prompts), by typing `#` at the start of the box, or by sending `/prompt` on its own. Choosing a
  prompt with fields shows a small form (file fields offer the project's files, `{{name|default}}` pre-fills a value,
  `{{name:file}}` makes any field a file field) and then fills the message box; nothing is sent until you press Send. Keep a
  prompt for this project (`.axon/prompts/`) or for every project (`<AXON_HOME>/prompts/`) as plain markdown files; search,
  edit, delete, and import or export all your prompts as one file from the dialog. A few starters come built in and can be
  removed (More > Bring back removed starters). Keys and passwords are refused, and a name that is already a command, custom
  command or skill is refused, so a prompt is never mistaken for a command. In the terminal, `/prompt` opens the same list
  as a menu; `/prompt <name> key=value`, `save`, `delete`, `show`, `export`, `import` and `restore-starters` work typed.
  Prompts are text for the message box. Custom commands (`.axon/commands/*.md`, run as `/name`) and skills (which Axon can
  pick up by itself) stay separate: they act, a prompt only types for you.
- **Checkpoints.** The Changes panel has a Checkpoints section: one row per turn in which Axon edited files, with the time,
  the prompt that led to it and the files with `+` and `-` line counts. Click a checkpoint for its files and a file for its
  diff. "Restore to before this" lists exactly which files will change (put back, recreated or deleted) and asks first. It is
  saved as a new checkpoint, so "Undo this restore" is one click. A file you changed outside Axon since Axon last wrote it is
  a conflict: Axon never overwrites it silently; you can keep your version and restore the rest, or overwrite it (your
  version is kept in the new checkpoint). Restoring waits until Axon finishes its turn. In the terminal, `/rewind list`,
  `/rewind show <n>` and `/rewind restore <n> [--keep-mine|--overwrite]` do the same.

## Languages and accessibility

**Interface language.** Settings > Appearance > Language switches the app between English, Telugu (తెలుగు) and Hindi (हिन्दी).
The choice is saved in this browser and applies at once. The app frame is translated first: the left rail, top bar, right
toolbar, composer, menus, Progress panel, turn header words, common buttons and dialogs, empty states and Settings. Long pages
(Usage, Sessions, Extensions, Insights) are only partly translated and fall back to English word by word. Code, file paths,
tool output, terminal text and the model's replies are never translated. **Reply in this language** (off by default) adds one
line to the system prompt asking the model to write in the interface language; it is the `replyLanguage` setting and also
applies to the terminal. The terminal itself stays English: command descriptions and `/` command output are not translated.
The Telugu and Hindi text was written without a native reviewer: please have a native reader check it.

To add or fix a string, edit `src/axon/features/i18n/en.json`, `te.json` and `hi.json` (same key in all three; `{name}` marks a
value filled in at run time). Page code can call `t('key')`, or just write the English text: any text, title, `aria-label` or
placeholder that equals a catalog string is swapped. `python -m axon.features.dash_js_i18n --missing` prints the coverage.

**Accessibility.** There is a skip link and landmarks (banner, navigation, main, complementary, status); every button has a name
and decorative icons are hidden from screen readers; the tab strip, page tabs, menus, dialogs and the file tree follow the
standard keyboard patterns (arrows, Home/End, Escape; dialogs trap Tab and give focus back; menus open on the first item).
Turn state ("working", "finished") is announced politely and the streamed reply is not read out token by token. Focus is
always visible, controls are at least 24 by 24 pixels, the system's reduced-motion setting is honoured (Appearance > Animations
> On overrides it), and the page reflows without horizontal scrolling at 200% zoom.

## Security model

The server is meant for you alone on your own machine.

- **Loopback only.** It binds to `127.0.0.1` and there is no option to listen on any other address. Asking for
  another host raises an error.
- **Private link.** The link carries a random token (`?t=...`). The server swaps it for an `HttpOnly`,
  `SameSite=Strict` cookie and redirects, so the token does not stay in the address bar. Every request needs the
  cookie. Anyone who has the link before it is used, or can read your browser's cookies, has the access
  described below, so do not share it.
- **Request checks.** Requests must carry a loopback `Host` header (a defence against DNS rebinding). Every POST
  must also come from the same origin and carry a JSON body (a defence against cross-site requests).
- **No outside loads.** The page, its script, styles, icon and colours are built into Axon and served by the
  server itself. The content security policy allows only same-origin resources, so the app works without a
  network connection and loads nothing from a CDN or font host.
- **Untrusted text.** Transcript text is escaped before any HTML is built, and secrets are redacted from
  what the server sends. Session ids are looked up in an index, never joined into a path.
- **Replies do not auto-approve unless you say so.** A reply sent from the page runs in `default`, `plan` or
  `acceptEdits` mode, and each tool approval is shown in the page. Approvals time out after 5 minutes. The mode
  menu also offers "Runs without asking" (bypass), shown in red. Choosing it asks for one confirmation per
  chat; deny rules and the always-protected locations still apply.
- **Sharing.** Export or share writes a file on this computer and nothing is uploaded. No screen offers an upload any more;
  the server keeps one confirmation-gated secret-gist route from before, which only an explicit request with the previewed
  file's hash can use.

### The Terminal tab is a full shell on your computer

The Terminal tab starts a real shell (your `$SHELL`) or the Axon program in a pseudo-terminal, in the project
folder, and sends it every key you type. It is the same as a terminal window: **it can run any command as you.**
It does not go through Axon's permission rules, the shell guard or the Bash sandbox. The page says so on the tab.
Its only protection is the access control above, so anything that can reach the app with a valid cookie can
run commands on the computer. Close terminals you are not using (up to 8 can be open at once). The Terminal tab
needs a pseudo-terminal, which Python provides on macOS and Linux only; on Windows the line-based Shell panel
is available instead.

The Shell panel is more limited: it runs one command at a time, applies the shell guard (destructive commands
and credential access are refused) and, when enabled, the Bash sandbox. As with the Bash tool, the guard reads
command text only; see [Shell guard and the sandbox](architecture.md#shell-guard-and-the-sandbox).

## Known limits

- The app is in development; see the status note above.
- If a tab shows "signed out", run `axon open` or `/open` again for a fresh link.
- A page attached to a running terminal session cannot change folder; run `axon open --workspace DIR` for
  another project.
