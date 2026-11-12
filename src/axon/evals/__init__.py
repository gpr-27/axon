"""``axon eval`` / ``/eval``: an evaluation harness for the coding agent.

Tasks (JSON, JSONL or TOML) describe a throwaway workspace, a prompt and exact checks; the runner drives the real
agent loop in a fresh temp dir for every model x effort x repeat, grades with exact checks first and an optional
LLM rubric second (the /goal grader), and stores each run under ``~/.axon/evals/<run-id>/``.
"""
