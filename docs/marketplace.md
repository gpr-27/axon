# Marketplace

`/marketplace` (aliases `/connectors` and `/store`) lets you find and install three kinds of extras.
The same commands run outside a session as `axon marketplace ...`.

| Tab | What it installs | Where it goes |
|---|---|---|
| Connectors | MCP servers | `~/.axon/mcp.json` (user), `<project>/.mcp.json` (project) or `<project>/.axon/mcp.json` (local) |
| Skills | `SKILL.md` skills | `~/.axon/skills/<name>/` or `<project>/.axon/skills/<name>/` |
| Plugins | Axon plugins | `~/.axon/plugins/<name>/` or `<project>/.axon/plugins/<name>/` |

Each tab has two views. **Discover** shows cards (name, a check mark for entries published by the
vendor or maintainer, a one-line description, "by publisher", and `+` or a check for installed).
**Yours** shows a table of what you have installed: name, type (Web for remote servers, Local for
servers started on your machine) and status.

## Using it

Run `/marketplace` in a terminal to open the browser. Type `c`, `s` or `p` to change tab, `y` or `d` for
Yours or Discover, `/text` to search, `g <category>` to filter, `f` to show only free entries,
`i <id>` to install, `r <id>` to remove, `v <id>` for details, `a` to add a custom connector, `n` to
create a skill or plugin, and `q` to leave.

Without a terminal (scripts, `axon -p`, CI) every command is non-interactive and prints plain text.

```text
/marketplace list [connectors|skills|plugins] [--installed|--discover] [--free] [--category C]
                  [--sort name|category|installed|tokens|updates] [--json]
/marketplace search <words> [--tab skills] [--category C] [--sort ...]
/marketplace info <id>
/marketplace categories [tab]
/marketplace install <id> [--scope user|project|local] [--env NAME=VALUE] [--keep] [--no-check] [--timeout 20]
/marketplace remove <id>          /marketplace enable <id>          /marketplace disable <id>
/marketplace add <https://url | command args> --name <name> [--transport http|sse]     a connector
/marketplace add <git url | owner/repo | path> [--marketplace]     your own marketplace (plugins and/or skills)
/marketplace sources · refresh [name] · remove-source <name>
/marketplace updates · update <id> [--yes]
/marketplace create-skill [--name N --description D --trigger T --instructions TEXT --script FILE]
/marketplace create-plugin <name> [--scope user|project]
```

If an id exists in more than one tab, add `--tab connectors|skills|plugins`.

Each card shows whether the entry is installed (`✓`), whether the catalog pins a different version than the
one you have (`update available`) and, for skills, `~N tokens/turn`: the listing line the model sees in every
prompt while the skill is installed, counted the way `/skill-doctor` counts it (the body is only read when
the skill is used). `--sort tokens` puts the cheapest first, `--sort updates` the ones with updates,
`--sort installed` what you already have. Installing a skill lists its files first (executable scripts
flagged) and asks; updating shows the changes and asks (`--yes` skips the question in a script).

### Your own marketplaces

`/marketplace add <git url | owner/repo | path>` adds a source for good (state: `~/.axon/plugins/marketplaces.json`).
It can be a plugin marketplace (`.claude-plugin/marketplace.json`, the same as `/plugin marketplace add`) or
just a folder or repository of skills (`SKILL.md` folders, directly or under `skills/`); a skills-only source
needs no marketplace file. Its plugins and skills appear in `list` and `search` under the category
`Custom` (or the category the marketplace gives), install like the built-in ones, and are never marked
verified. `/marketplace refresh` pulls changes. A URL that starts with `http(s)://` and is not a repository
or a `.json` file is still taken as an MCP connector; add `--marketplace` or `--connector` to decide.

The commands that control what you already installed are listed under [Controlling what is
installed](#controlling-what-is-installed); the automatic behaviour is under [Automation](#automation).

## Installing a connector

1. The entry is turned into a normal MCP server definition and written with the same code `/mcp add`
   uses, so `/mcp` shows and manages it afterwards. Installing twice does nothing the second time.
2. If the connector needs keys, Axon uses a value you passed with `--env`, one already exported or stored
   in `~/.axon/.env`, or asks you (hidden input). New keys are saved to `~/.axon/.env` with
   owner-only permissions. The MCP config only stores a `${NAME}` reference, never the key.
   Without a terminal and without the value, the install stops before writing anything.
3. Axon connects once and lists the tools. The check has a hard time limit (default 20 seconds,
   change with `--timeout`), so an install never hangs.
4. If the server cannot be reached, the message says which URL or command failed and the config
   is rolled back. Pass `--keep` to keep the entry anyway. Saved keys are not deleted.
5. Servers that use OAuth finish installing and tell you to run `/mcp login <id>`.

`remove` deletes the entry from your config files; keys stay in `~/.axon/.env`.

## Controlling what is installed

Everything below works on connectors, skills and plugins where it makes sense, from three places that
share one implementation: `/marketplace ...`, the older `/mcp`, `/skills` and `/plugin` commands, and
`axon marketplace ...` outside a session. Inside a session a change to a connector is applied at once
(the connector is reconnected, or disconnected when you remove it); skills and plugins are picked up
by the next prompt.

```text
/marketplace status [--check] [--all] [--json]
/marketplace enable|disable <id> [--scope project]
/marketplace test <id> [--timeout S]
/marketplace reconnect <id|all>
/marketplace logs <id> [lines]
/marketplace scope <id> user|project|local
/marketplace env <id> NAME=VALUE ... [--unset NAME] [--literal]
/marketplace timeout <id> <seconds>
/marketplace tool <id> [<tool>|*] allow|ask|deny|reset [--global]
/marketplace export [file] [--include-secrets]
/marketplace import <file> [--dry-run] [--overwrite]
```

| Task | Connectors | Skills | Plugins |
|---|---|---|---|
| list, info | `list --installed`, `info` | same | same |
| install, remove | yes | yes | yes |
| enable, disable | stored in the MCP config (or the project state for a shared `.mcp.json`) | `skillOverrides` in `settings.json`; a disabled skill disappears from the skill list and the system prompt | plugin settings |
| test | connects once and lists the tools | validates `SKILL.md` | validates the plugin folder |
| reconnect, logs, env, timeout, tool rules | yes | not applicable | `/plugin configure` for settings |
| scope | `user`, `project` or `local` | `user` or `project` | `user` or `project` |

**status** prints one table with a row for every connector, skill and plugin: whether it is enabled,
whether it is connected, how many tools it offers, the last error and the scope it lives in. Inside a
session the connected and tools columns come from the live connections. Outside a session nothing is
running, so they show `-`; add `--check` to connect to every enabled connector once (with a time limit).
`--json` prints the same rows as JSON. The built-in skills are left out unless you add `--all`.

**test** connects once, lists the tools and tells you how long it took. A failed test records the error
(shown by `status`) but is not counted as a failed connection for [auto-disable](#automation). For a
server that needs you to sign in it says `/mcp login <id>`.

**env** sets variables and keys of a connector. A name that looks like a secret (`KEY`, `TOKEN`,
`SECRET`, `PASSWORD`, ...) is saved in `~/.axon/.env` with owner-only permissions, and the connector config
keeps only `${NAME}`. Give a bare `NAME` to be asked for the value with hidden input, or `--literal` to write
the value into the config as typed. For a remote connector the key must be used by a header or the url
(`Bearer ${NAME}`); Axon tells you when it is not.

**scope** moves the definition to another file: `user` is `~/.axon/mcp.json`, `project` is `.mcp.json` (meant to
be committed; Axon warns when it holds literal keys) and `local` is `.axon/mcp.json`. Connectors from a plugin
or your organization cannot be moved.

**tool** writes a permission rule for `mcp__<server>__<tool>`: `allow` runs without asking, `ask` always asks,
`deny` blocks. Use `*` (or leave the tool out) for every tool of the server. Switching a tool replaces its earlier
rule. Rules go to `.axon/settings.local.json`, or to `~/.axon/settings.json` with `--global`. `reset` removes the
rules so the permission mode decides again, and `tool <id>` lists the rules of a server.

**timeout** sets the `timeout` of the connector in seconds.

**export** writes the whole setup as one JSON file (`axon-setup.json` in the project unless you name a file): connectors with their scope, installed skills (with their
files), disabled skills, plugins, the `mcp__` permission rules and the automation settings. Keys never go
into the file: secret values become `${NAME}` references and the file lists them under `needs_keys`. Pass
`--include-secrets` to keep them (the file is then owner-only). Secrets typed into a command argument cannot be
recognised, so read the file before sharing it. **import** adds what is missing, skips what is already
installed, and never replaces a different connector or skill unless you pass `--overwrite`. `--dry-run` shows
what would happen. Plugins from the catalog are installed again; others are listed with the command to run.
`auto_install` and `auto_update` are never taken from a file.

## Automation

Axon can look after connectors and skills for you. All of it is **off until you turn it on**:

```text
/marketplace auto on          turn the master switch on
/marketplace auto status      what is on, with every setting and its value
/marketplace auto set <name> <value>
/marketplace auto off
```

The settings live in the `automation` block of `~/.axon/settings.json` (or `[automation]` in
`~/.axon/config.toml`). A project's `.axon/settings.json` may change the harmless keys, but never
`auto_install` or `auto_update`, so a repository you clone cannot install things for you.

```json
{ "automation": { "enabled": true, "auto_disable_after": 3, "auto_skills": true } }
```

| Key | Default | What it does |
|---|---|---|
| `enabled` | `false` | master switch; nothing below happens while it is off |
| `auto_connect` | `true` | connect enabled connectors in the background at startup |
| `startup_wait` | `2` | seconds the first prompt waits for connectors; slower ones finish in the background and their tools appear when ready |
| `connect_retries` | `3` | extra attempts when the first connection fails, after 2, 5 and 15 seconds |
| `auto_reconnect` | `true` | reconnect a connector that drops during the session (local ones too; remote ones always reconnect when automation is off) |
| `auto_disable_after` | `3` | switch a connector off after this many failed connection rounds in a row (`0` = never) |
| `auto_skills` | `true` | add the skills that match your prompt to that prompt |
| `max_auto_skills` | `2` | most skills added to one prompt |
| `suggest` | `true` | suggest connectors that fit the project, once per project |
| `auto_install` | `false` | install suggested connectors that need nothing more from you (user settings only) |
| `check_updates` | `true` | look for newer catalog entries, skills and plugins now and then |
| `update_interval_days` | `7` | days between update checks |
| `auto_update` | `false` | apply skill and plugin updates, never connectors (user settings only) |

Background work never prints into your prompt. It posts one-line notices that appear with your next prompt,
for example `MCP server 'x' was switched off after 3 failed connections (...). Turn it back on with:
/marketplace enable x`. Every failure of the automatic work is ignored, so it cannot slow down or break
startup.

**Connecting.** Connectors connect in parallel, in the background. After a failed first attempt Axon tries
again with growing pauses; a connector that needs you to sign in or has a broken config is not retried. A
connector that drops later is reconnected with growing pauses (five attempts, 1 to 16 seconds).

**Auto-disable.** A "failed round" is one whole connection attempt including its retries, or one dropped
connection whose reconnects all failed. The count is kept in `~/.axon/state/mcp-health.json` and resets after
a success. When it reaches `auto_disable_after`, Axon switches the connector off, disconnects it and posts a
notice. `/marketplace enable <id>` turns it back on and clears the count; `status` and `info` show that Axon
switched it off. Failures of `/marketplace test` and manual reconnects are not counted.

**Skills by trigger.** A skill matches a prompt when one of its trigger phrases is in the prompt (the
`triggers:` list in `SKILL.md`, or the phrases the skill creator writes after "Use when the user mentions:"),
when the built-in intent words or a `/name` mention of `SkillManager.auto_match` find it, or when at least three
distinctive words of its description appear in your prompt. Workspace files that match the skill's `paths:`
add weight. The best matches (at most `max_auto_skills`) are added to the prompt, each one once per session and
each cut at 6,000 characters, and a line tells you which skills were added. Skills that only you may run
(`disable-model-invocation`) and disabled skills are never added.

**Suggestions.** At startup Axon looks for: a `github.com` git remote; dependencies in `package.json`
(`@supabase/supabase-js`, `stripe`, `@clerk/*`, `prisma`, `@sentry/*`, `mongodb`, `mongoose`, `@neondatabase/serverless`,
`@upstash/*`, `@vercel/*`, `wrangler`, `@playwright/test`, `@linear/sdk`, `@notionhq/client`) and in `requirements.txt` /
`pyproject.toml`; files such as `vercel.json`, `render.yaml`, `wrangler.toml`, `prisma/schema.prisma`,
`supabase/config.toml` and Sentry config; and `mongodb://` strings in `.env`, `.env.example` and similar files (only
the fact that one exists is used, never its value). Fitting catalog connectors that are not installed are
suggested once per project (`~/.axon/state/connector-suggestions.json`); `/marketplace suggest` shows them at any
time and `suggest --reset` makes the project be told again. Nothing is installed unless you install it, or
`auto_install` is on, which installs into the project's `local` scope only the connectors whose required keys
are already available.

**Updates.** Every `update_interval_days` the check compares your catalog connectors with the catalog (a
changed address), your catalog skills with their copy on GitHub, and plugins with the plugin marketplaces you
added. It only reports. `/marketplace updates` runs the check now, `/marketplace update <skill|plugin>` applies
one update, and with `auto_update` on, skills and plugins are updated by the periodic check. Connectors are
never changed by an update.

## Creating things

`create-skill` asks for a name, a description, optional trigger phrases, the instructions and optional
script files, then writes `SKILL.md` and checks it with the same validator `/plugin validate` uses.
`create-plugin` writes a plugin folder with a manifest and an example command and validates it.

## The catalog

The catalog is three JSON files in `src/axon/marketplace/data/`: `connectors.json`, `skills.json` and
`plugins.json`. `axon.marketplace.catalog` loads them and rejects missing fields, duplicate ids and
non-https URLs. Axon shows only the number of entries it really has; there is no made-up total.

**Skills** come from Anthropic's public skills repository (`anthropics/skills`, 19 skills), plus `create-skill`, Axon's own skill creator (20 entries). **Plugins** come
from Anthropic's official plugins marketplace (`anthropics/claude-plugins-official`): its own plugins plus
a selection of partner plugins that the marketplace lists. Every skill and plugin entry is pinned to the full
commit sha it was checked at (`sha`, or `source.sha` for plugins); a plugin id that would clash with a connector
(`github`, `slack` ...) is listed as `<id>-plugin`. `verified` means exactly one thing: the entry comes from
that official Anthropic marketplace or skills repository. Nothing else earns it, and marketplaces you add
yourself never have it. Entries were checked with status-only requests (the pinned commit and folder exist);
nothing was downloaded or run to check them. To add one, copy its `source` and `sha` from the official
`marketplace.json`, and confirm that the commit and path exist.

A connector entry has: `id`, `name`, `publisher`, `description`, `category`, `transport`
(`http`, `sse` or `stdio`), `url` or `command` plus `args`, `auth` (`none`, `oauth` or `api_key`),
`env_vars` (name, help, required), `headers` (templates such as `Bearer ${NAME}`), `docs_url`,
`verified` (published by the vendor or the maintainers of the protocol), `free` and `free_note`.

Entries were checked against the publisher's own documentation; remote endpoints were probed
for a live MCP response (HTTP 401 or 405 without credentials), and npm or PyPI packages were checked
to exist. `free` is false where the documentation does not promise a free tier. Catalog data can become
out of date: if a server moved, `install` shows the connection error and rolls back.

Some OAuth servers do not accept automatic client registration, so signing in needs a client ID of your own
(`/mcp add ... --client-id ID`). This applies to the Google Workspace servers (`gmail`, `google-calendar`,
`google-drive`, `google-docs`, `google-sheets`, `google-slides`; Google lists them as a Developer Preview and
each one has its own endpoint and tools) and to `slack` (needs a Slack app that is directory-published or
internal, plus workspace admin approval). Each entry's `free_note` says what is needed.

Skills in the catalog are downloaded as a whole folder (see "Skills installed from the marketplace" in
[extending.md](extending.md)). Catalog plugins are fetched from their git source, at the pinned commit, into the plugins folder.

To add your own entry, add an object to the right JSON file and run `pytest tests/test_marketplace_catalog.py`.
