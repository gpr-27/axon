"""Regressions found by driving the real terminal app (print mode, piping, tips)."""


def test_print_mode_text_has_no_tip_lines(axon_env):
    """`axon -p ... > answer.txt` must hold the turn, not a usage tip meant for a person at the prompt."""
    axon_env.llm.enqueue([{"text": "PRINT-OK"}])
    r = axon_env.run(["-p", "say hi"])
    assert r.code == 0 and "PRINT-OK" in r.stdout
    assert "Tip:" not in r.stdout, r.stdout


def test_piped_stdin_without_p_has_no_tip_lines(axon_env):
    axon_env.llm.enqueue([{"text": "PIPE-OK"}])
    r = axon_env.run([], input="hello\n")
    assert r.code == 0 and "PIPE-OK" in r.stdout
    assert "Tip:" not in r.stdout, r.stdout


def test_message_typed_during_a_reply_is_listed_in_the_footer_not_printed_into_the_reply(axon_env):
    """The queue notice used to be printed in the middle of the card that was streaming."""
    import pytest
    pytest.importorskip("pty")
    axon_env.llm.enqueue([{"text": "first part "}, {"delay": 2.0}, {"text": "LAST-PART"}], [{"text": "SECOND-OK"}])
    with axon_env.pty() as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("go\r")
        session.expect(r"first part", 15)
        session.send("second one")
        session.send("\r")
        session.expect(r"queued \(1\)", 10)                      # shown in the pinned footer
        session.expect(r"LAST-PART", 15)
        session.expect(r"SECOND-OK", 20)
        text = session.screen
    assert "📥 Queued #1" not in text, text
    assert text.count("[Queued #1]") == 1


def test_keys_typed_while_the_permission_prompt_appears_do_not_answer_it(axon_env, tmp_path):
    """Typing the next message while the model works used to leak a `y` into the permission prompt (allowing the call)."""
    import time

    import pytest
    pytest.importorskip("pty")
    axon_env.llm.enqueue([{"text": "I will write. "}, {"tool": "Write", "args": {"file_path": "out.txt", "content": "x\n"}}],
                         [{"text": "after"}])
    with axon_env.pty(["--mode", "default"]) as session:
        session.expect(r"Ask anything|›|>", 20)
        session.send("write it\r")
        session.expect(r"Allow Write\?", 15)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.15:                      # right as the question appears
            session.send("y")
            time.sleep(0.03)
        time.sleep(0.8)
        session.drain(0.3)
        assert not (axon_env.workspace / "out.txt").exists(), session.screen[-1500:]
        session.send("n")
        session.expect(r"after|declined|denied|No", 10)
