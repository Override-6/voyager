import base64

import httpx2
import pytest

from conftest import run
from voyager.tools import ToolContext, ToolError, tools_for
from voyager.tools import web
from voyager.tools.websearch import (SearchError, parse_bing, parse_brave, parse_ddg, reset_cooldowns, search)

BING_URL = "https://bing.example/1"
DDG = """<html><body>
<div class="result results_links results_links_deep web-result"><div class="links_main result__body">
 <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.org%2Fa&rut=x">Doc  A</a></h2>
 <a class="result__snippet" href="x">Snippet <b>A</b> text</a></div></div>
<div class="result results_links result--ad"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.example%2F">Ad</a></div>
<div class="result results_links"><a class="result__a" href="https://direct.example.org/b">Doc B</a><a class="result__snippet">Snippet B</a></div>
</body></html>"""
BRAVE = """<html><body><div class="snippet svelte-x" data-type="web"><div class="result-content"><a href="https://brave.example/1" class="l1">
<div class="title search-snippet-title">Brave One</div></a><div class="generic-snippet"><div class="content">Brave snippet one</div></div></div></div>
<div class="snippet" data-type="news"><a href="https://news.example/">Not a web result</a></div></body></html>"""
BING = f"""<html><body><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{base64.urlsafe_b64encode(BING_URL.encode()).decode().rstrip('=')}&x=1">Bing One</a></h2>
<div class="b_caption"><p>Bing snippet</p></div></li></body></html>"""
ARTICLE = ("<html><head><title>My Article</title><script>var secret_js = 1;</script></head><body><nav>MENU HOME ABOUT</nav><article>"
           "<h1>Main Heading</h1>" + "".join(f"<p>Paragraph {i}: asyncio makes concurrent code easier to write and reason about in Python.</p>" for i in range(12)) +
           '<h2 id="sec">Section<a class="headerlink" href="#sec">¶</a></h2><p>See <a href="https://other.example/page">the other page</a> for more details on this topic.</p>'
           "</article><footer>COPYRIGHT FOOTER</footer></body></html>")


@pytest.fixture(autouse=True)
def _clean():
    reset_cooldowns()


def mock_web(monkeypatch, handler):
    monkeypatch.setattr(web, "make_client", lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=True))


def ctx():
    from pathlib import Path
    return ToolContext(cwd=Path("."))


def test_parsers():
    ddg = parse_ddg(DDG)
    assert [(h.title, h.url, h.snippet) for h in ddg] == [("Doc A", "https://docs.example.org/a", "Snippet A text"), ("Doc B", "https://direct.example.org/b", "Snippet B")]  # ad skipped, redirect decoded
    assert [(h.title, h.url, h.snippet) for h in parse_brave(BRAVE)] == [("Brave One", "https://brave.example/1", "Brave snippet one")]
    assert [(h.title, h.url, h.snippet) for h in parse_bing(BING)] == [("Bing One", BING_URL, "Bing snippet")]


def test_engine_fallback_and_cooldown(monkeypatch):
    calls = {"ddg": 0, "brave": 0}

    def handler(req: httpx2.Request):
        if "duckduckgo" in req.url.host:
            calls["ddg"] += 1
            return httpx2.Response(202, text="<html>anomaly: please complete the captcha</html>")
        calls["brave"] += 1
        return httpx2.Response(200, text=BRAVE)

    async def go():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as c:
            assert (await search(c, "q", 5))[0] == "Brave" and calls == {"ddg": 1, "brave": 1}
            assert (await search(c, "q", 5))[0] == "Brave" and calls == {"ddg": 1, "brave": 2}  # DuckDuckGo skipped: cooling down
    run(go())


def test_all_engines_failing_raises_with_reasons():
    async def go():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(503))) as c:
            with pytest.raises(SearchError) as e:
                await search(c, "q", 5)
        assert all(n in str(e.value) for n in ("DuckDuckGo", "Brave", "Bing"))
    run(go())


def test_web_search_tool_formats_results(monkeypatch):
    mock_web(monkeypatch, lambda r: httpx2.Response(200, text=DDG))
    out = run(web.WebSearch().run({"query": "asyncio", "max_results": 1}, ctx()))
    assert "(via DuckDuckGo)" in out and "1. Doc A" in out and "https://docs.example.org/a" in out and "Doc B" not in out and "web_fetch" in out
    with pytest.raises(ToolError, match="recency"):
        run(web.WebSearch().run({"query": "x", "recency": "decade"}, ctx()))
    mock_web(monkeypatch, lambda r: httpx2.Response(500))
    with pytest.raises(ToolError, match="web search failed"):
        run(web.WebSearch().run({"query": "x"}, ctx()))


def test_fetch_html_extracts_main_content_and_drops_noise(monkeypatch):
    mock_web(monkeypatch, lambda r: httpx2.Response(200, text=ARTICLE, headers={"content-type": "text/html; charset=utf-8"}))
    out = run(web.WebFetch().run({"url": "https://site.example/post"}, ctx()))
    assert "Untrusted web content" in out and "URL: https://site.example/post" in out and "Title: My Article" in out
    assert "Main Heading" in out and "Paragraph 3" in out and "[the other page](https://other.example/page)" in out
    assert "secret_js" not in out and "MENU HOME" not in out and "¶" not in out and "#sec" not in out


def test_fetch_paging_types_errors_and_size_cap(monkeypatch):
    big = "".join(f"line {i:05d}\n" for i in range(1000))  # 11,000 chars
    routes = {
        "/big.txt": httpx2.Response(200, text=big, headers={"content-type": "text/plain"}),
        "/data.json": httpx2.Response(200, text='{"a": 1}', headers={"content-type": "application/json"}),
        "/doc.pdf": httpx2.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
        "/pic.png": httpx2.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}),
        "/missing": httpx2.Response(404),
        "/long.txt": httpx2.Response(200, text="".join(f"line {i:05d}\n" for i in range(3000)), headers={"content-type": "text/plain"}),
        "/huge": httpx2.Response(200, content=b"x" * (web.MAX_BYTES + 5000), headers={"content-type": "text/plain"}),
    }
    mock_web(monkeypatch, lambda r: routes[r.url.path])
    fetch = web.WebFetch()
    first = run(fetch.run({"url": "https://s.example/big.txt", "max_chars": 500}, ctx()))
    assert "showing 0-500" in first and "start=500" in first and "line 00000" in first
    second = run(fetch.run({"url": "https://s.example/big.txt", "start": 500, "max_chars": 500}, ctx()))
    assert "showing 500-1000" in second and "line 00000" not in second
    assert "no more content" in run(fetch.run({"url": "https://s.example/big.txt", "start": 999999}, ctx()))
    assert '{"a": 1}' in run(fetch.run({"url": "https://s.example/data.json"}, ctx()))
    for path, msg in (("/doc.pdf", "PDF"), ("/pic.png", "not a text page"), ("/missing", "HTTP 404")):
        with pytest.raises(ToolError, match=msg):
            run(fetch.run({"url": "https://s.example" + path}, ctx()))
    with pytest.raises(ToolError, match="http"):
        run(fetch.run({"url": "ftp://s.example/x"}, ctx()))
    maxed = run(fetch.run({"url": "https://s.example/long.txt", "max_chars": 10**6}, ctx()))  # a model asking for "everything"
    assert "showing 0-12000" in maxed and "start=12000" in maxed and len(maxed) < 20_000  # paging hint must survive the tool-result cap
    assert "download capped at 3 MB" in run(fetch.run({"url": "https://s.example/huge"}, ctx()))


def test_tools_available_to_every_agent():
    assert {"web_search", "web_fetch"} <= set(tools_for(True)) and {"web_search", "web_fetch"} <= set(tools_for(False))
