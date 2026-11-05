"""The browser page shown after an MCP sign-in: branded, server named, values escaped, code cleared from the URL."""
from axon.mcp.callback_page import failure_page, success_page


def test_success_page_names_the_server_and_shows_next_steps():
    page = success_page("vercel")
    assert "Authorization received" in page and "vercel" in page
    assert "<svg" in page and "/mcp" in page
    assert "history.replaceState" in page


def test_failure_page_escapes_provider_text():
    page = failure_page("<b>bad</b>", "<script>alert(1)</script>", "my<srv>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "&lt;b&gt;bad&lt;/b&gt;" in page and "my&lt;srv&gt;" in page


def test_pages_work_without_a_server_name():
    assert "the server" in success_page("")
    assert "/mcp login &lt;server&gt;" in failure_page("no code")


def test_pages_make_no_external_requests():
    for page in (success_page("x"), failure_page("e")):
        assert "http://" not in page and "https://" not in page
