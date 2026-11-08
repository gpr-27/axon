"""A tiny fake language server for the LSP tool tests (stdio, Content-Length framed JSON-RPC).

Behaviour: every line containing BADTOKEN is an error diagnostic; definition of a word is the line that has
"def <word>"; references are all occurrences of the word; hover answers "hover:<word>". Events are appended to
the file named by FAKE_LSP_LOG (one JSON object per line)."""
import json
import os
import re
import sys

LOG = os.environ.get("FAKE_LSP_LOG")
docs = {}


def log(ev):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(ev) + "\n")


def send(obj):
    body = json.dumps(obj).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read():
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


def word_at(text, line, ch):
    ln = text.split("\n")[line]
    for m in re.finditer(r"\w+", ln):
        if m.start() <= ch <= m.end():
            return m.group(0)
    return ""


def publish(uri):
    text = docs[uri]
    diags = []
    for i, ln in enumerate(text.split("\n")):
        j = ln.find("BADTOKEN")
        if j >= 0:
            diags.append({"range": {"start": {"line": i, "character": j}, "end": {"line": i, "character": j + 8}},
                          "severity": 1, "code": "E1", "source": "fake", "message": "bad token found"})
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diags}})


def loc(uri, i, j, n):
    return {"uri": uri, "range": {"start": {"line": i, "character": j}, "end": {"line": i, "character": j + n}}}


while True:
    m = read()
    if m is None:
        break
    meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
    log({"m": meth})
    if meth == "initialize":
        if os.environ.get("FAKE_LSP_CONFIG_REQUEST"):
            send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"hoverProvider": True}}})
    elif meth == "textDocument/didOpen":
        d = p["textDocument"]
        docs[d["uri"]] = d["text"]
        publish(d["uri"])
    elif meth == "textDocument/didChange":
        docs[p["textDocument"]["uri"]] = p["contentChanges"][-1]["text"]
        publish(p["textDocument"]["uri"])
    elif meth in ("textDocument/definition", "textDocument/references", "textDocument/hover"):
        uri = p["textDocument"]["uri"]
        text = docs[uri]
        w = word_at(text, p["position"]["line"], p["position"]["character"])
        if meth == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": "markdown", "value": "hover:" + w}} if w else None})
        elif meth == "textDocument/definition":
            res = None
            for i, ln in enumerate(text.split("\n")):
                k = ln.find("def " + w)
                if w and k >= 0:
                    res = loc(uri, i, k + 4, len(w))
                    break
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        else:
            res = []
            for i, ln in enumerate(text.split("\n")):
                for mm in re.finditer(r"\b%s\b" % re.escape(w), ln) if w else []:
                    res.append(loc(uri, i, mm.start(), len(w)))
            send({"jsonrpc": "2.0", "id": mid, "result": res})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif meth == "exit":
        break
    elif mid is not None and meth is None:
        log({"response_to_server": mid, "result": m.get("result")})
