"""
Built-in slash commands (compatibility facade).

Each command is implemented exactly once in a themed module and registered in
:mod:`axon.commands.registry`; importing this module loads them all. Public names
used across the code base and tests are re-exported here.
"""
from __future__ import annotations

from axon.commands.registry import (  # noqa: F401
    ALIASES, COMMANDS, Command, CommandResult, command, dispatch_command, list_command_names, render_help,
    resolve_builtin,
)
from axon.commands import (  # noqa: F401  (import for registration side effects)
    agent_cmds, code_cmds, context_cmds, endpoint_cmds, insight_cmds, keys_cmds, memory_cmds, model_cmds, perm_cmds, prefs_cmds,
    session_cmds, settings_cmds, system_cmds, workspace_cmds,
)
from axon.commands.agent_cmds import (  # noqa: F401
    handle_agents, handle_explain, handle_hooks, handle_inspect, handle_main, handle_mcp, handle_plugin,
    handle_reload_plugins, handle_skills, handle_subagents, handle_tasks, handle_tools, run_mcp_prompt, run_skill,
)
from axon.commands.code_cmds import (  # noqa: F401
    handle_btw, handle_copy, handle_diff, handle_find, handle_output, handle_paste, handle_queue, handle_review,
    handle_test, handle_todos,
)
from axon.commands.context_cmds import (  # noqa: F401
    handle_compact, handle_context, handle_cost, handle_faq, handle_payload, handle_pricing, handle_window,
)
from axon.commands.memory_cmds import (  # noqa: F401
    handle_init, handle_learn, handle_memory, handle_optimize, handle_prompt, quick_memory,
)
from axon.commands.endpoint_cmds import handle_endpoint  # noqa: F401
from axon.commands.keys_cmds import handle_keys  # noqa: F401
from axon.commands.settings_cmds import handle_settings  # noqa: F401
from axon.commands.model_cmds import (  # noqa: F401
    handle_effort, handle_mode, handle_model, handle_plan, handle_provider, handle_thinking,
    handle_trace, save_or_update_env_key, switch_model,
)
from axon.commands.perm_cmds import handle_permissions  # noqa: F401
from axon.commands.prefs_cmds import (  # noqa: F401
    handle_color, handle_fast, handle_goal, handle_output_style, handle_release_notes, handle_statusline,
    handle_terminal_setup, handle_theme, handle_vim,
)
from axon.commands.workspace_cmds import handle_add_dir, handle_cd, handle_loop, purge  # noqa: F401
from axon.commands.session_cmds import (  # noqa: F401
    handle_branch_cmd as handle_branch, handle_clear, handle_delete, handle_exit, handle_export, handle_history,
    handle_rename, handle_resume_cmd as handle_resume, handle_rewind, handle_sessions, handle_star, handle_tag,
)
from axon.commands.system_cmds import (  # noqa: F401
    handle_autocompact, handle_bug, handle_config, handle_doctor, handle_env, handle_help, handle_keybindings,
    handle_notify, handle_recap, handle_status, handle_statusbar,
)

# Backward-compatible names
handle_breakdown = handle_payload
