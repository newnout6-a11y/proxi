"""WebSearch for Claude Code and other Anthropic clients: the relay runs the search that the Retool route cannot.

Claude Code's WebSearch is Anthropic's server tool web_search_20250305 (later versions have the same name and shape): the
provider runs the searches itself. Retool's route runs client tools only, so the relay declared such a request refused (422).
With a search provider configured, a request that carries the server tool is declared upstream as an ordinary function tool,
the relay answers the model's calls itself (orchestrator), lets the model go on with the results, and tells the client in
Anthropic's server-tool format: `server_tool_use` and `web_search_tool_result` blocks and `usage.server_tool_use`, the shape
Claude Code reads. Without a provider the refusal stays.

The provider is Exa (POST /search). Its key comes from the environment variable that `web_search_key_env` names and never
from config.json, a trace or a journal line.
"""
import base64
import collections
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import apierr

# web_search_20250305, web_search_20260209, web_search_20260318: one tool, three versions of the provider's own features.
SERVER_TOOL = re.compile(r"web_search_\d{8}")
PROVIDERS = ("exa",)
# What a request may spend when its tool sets no max_uses, and the longest query one call may carry.
DEFAULT_MAX_USES = 10
QUERY_LIMIT = 1000
# What of a page reaches the model, and what of it travels in the opaque encrypted_content a client echoes back.
SNIPPET_LIMIT = 1500
RESULTS_LIMIT = 12000

# `text` is what the model reads of the page; `page_age` is how the provider words its date ("April 30, 2025") or None.
Result = collections.namedtuple("Result", "title url page_age text")

DESCRIPTION = ("Search the web for current information. Returns the most relevant pages with their titles, URLs and the "
               "passages that matched. Use it for recent events, current facts and anything that may have changed since "
               "your training. Write a specific, self-contained query. After the results arrive, answer from them and "
               "name the sources you used.")


class SearchError(Exception):
    """A search that failed: `code` is Anthropic's error_code for it, `detail` a short note for the journal."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def tool_definition(name, spec):
    """The function tool the model sees in place of the server tool; `spec` rides along for the orchestrator."""
    return {"type": "function", "function": {
        "name": name, "description": DESCRIPTION,
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "The search query"}},
                       "required": ["query"]},
        "x-relay-tool": "web_search", "x-relay-search": spec}}


def _domains(entry, where):
    """Bare domains of a list the tool carries; a scheme or a slash at the end is the model's habit, not an error."""
    if entry is None:
        return []
    if not isinstance(entry, list) or not all(isinstance(item, str) for item in entry):
        raise apierr.unprocessable(f"{where} must be an array of strings", param=where)
    found = []
    for item in entry:
        item = re.sub(r"^[a-z][a-z0-9+.-]*://", "", item.strip(), flags=re.IGNORECASE).rstrip("/")
        if item and item not in found:
            found.append(item)
    return found


def spec_of(tool, where):
    """What the orchestrator needs of the server tool entry: max_uses, the domain lists, the user's country."""
    max_uses = tool.get("max_uses")
    if max_uses is not None and (isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1):
        raise apierr.unprocessable(f"{where}.max_uses must be a positive integer", param=where + ".max_uses")
    allowed = _domains(tool.get("allowed_domains"), where + ".allowed_domains")
    blocked = _domains(tool.get("blocked_domains"), where + ".blocked_domains")
    if allowed and blocked:
        raise apierr.unprocessable(f"{where}: allowed_domains and blocked_domains cannot be used together", param=where)
    location = tool.get("user_location")
    country = location.get("country") if isinstance(location, dict) else None
    return {"max_uses": max_uses or DEFAULT_MAX_USES, "allowed_domains": allowed, "blocked_domains": blocked,
            "country": country.upper() if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) else None}


# -- what the model reads, and what the client keeps -------------------------------------------------------------------

def encode(result):
    """The opaque text a client sends back inside web_search_result.encrypted_content: the page text and where it came from."""
    raw = json.dumps({"u": result.url, "t": result.title, "x": result.text}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode(token, fallback=None):
    """The Result a block of ours carries; `fallback` (a block with url, title, page_age) stands in for one it does not."""
    block = fallback if isinstance(fallback, dict) else {}
    try:
        data = json.loads(base64.urlsafe_b64decode(str(token).encode("ascii")).decode("utf-8"))
        text = data.get("x") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise ValueError("not ours")
        return Result(str(data.get("t") or block.get("title") or ""), str(data.get("u") or block.get("url") or ""),
                      block.get("page_age"), text)
    except (ValueError, TypeError, UnicodeError):
        return Result(str(block.get("title") or ""), str(block.get("url") or ""), block.get("page_age"), "")


def render(results):
    """The tool result the model reads: one numbered entry per page."""
    if not results:
        return "No results."
    entries = []
    for index, result in enumerate(results, 1):
        lines = ["[%d] %s" % (index, result.title or result.url), result.url]
        if result.page_age:
            lines.append("Published: %s" % result.page_age)
        if result.text:
            lines.append(result.text)
        entries.append("\n".join(lines))
    text = "\n\n".join(entries)
    return text if len(text) <= RESULTS_LIMIT else text[:RESULTS_LIMIT] + "\n[results cut]"


def render_error(code):
    return "web_search error: %s" % code


def _host_matches(url, domain):
    """Whether a page is on `domain`: a bare domain with an optional path prefix, subdomains included (Anthropic's rule)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    name, _, prefix = domain.lower().lstrip("*.").partition("/")
    return bool(host) and (host == name or host.endswith("." + name)) and (
        not prefix or parts.path.lstrip("/").lower().startswith(prefix))


def within_domains(results, spec):
    """The results the tool's allowed_domains / blocked_domains let through; the relay checks them whatever the provider did."""
    allowed, blocked = spec.get("allowed_domains") or (), spec.get("blocked_domains") or ()
    return [result for result in results
            if (not allowed or any(_host_matches(result.url, domain) for domain in allowed))
            and not any(_host_matches(result.url, domain) for domain in blocked)]


def result_blocks(results):
    """The content of a web_search_tool_result block: one web_search_result per page, in Anthropic's shape."""
    return [{"type": "web_search_result", "url": result.url, "title": result.title, "page_age": result.page_age,
             "encrypted_content": encode(result)} for result in results]


def render_block_content(content):
    """The text the model reads for the `content` of a web_search_tool_result block a client sends back."""
    if isinstance(content, dict):
        return render_error(content.get("error_code") or "unavailable")
    if not isinstance(content, list):
        return "No results."
    return render([decode(item.get("encrypted_content"), item) for item in content if isinstance(item, dict)])


# -- the provider ------------------------------------------------------------------------------------------------------

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
           "December")


def _page_age(value):
    """Anthropic words a page's date as "April 30, 2025"; the provider gives an ISO timestamp."""
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return "%s %d, %d" % (_MONTHS[moment.month - 1], moment.day, moment.year)


class Exa:
    """POST {endpoint}/search with the key in x-api-key (https://exa.ai/docs/exa-spec.yaml: SearchRequest)."""

    def __init__(self, key, endpoint, results, timeout):
        self.key, self.endpoint, self.results, self.timeout = key, endpoint.rstrip("/"), results, timeout

    def __repr__(self):
        return "Exa(%s)" % self.endpoint  # never the key

    @staticmethod
    def _filters(domains):
        """Exa matches a hostname exactly; a wildcard takes its subdomains, which Anthropic's lists include."""
        found = []
        for domain in domains:
            found.append(domain)
            if "/" not in domain and not domain.startswith("*."):
                found.append("*." + domain)
        return found

    def body(self, query, spec):
        body = {"query": query, "type": "auto", "numResults": self.results,
                "contents": {"highlights": {"maxCharacters": SNIPPET_LIMIT}}}
        if spec.get("allowed_domains"):
            body["includeDomains"] = self._filters(spec["allowed_domains"])
        if spec.get("blocked_domains"):
            body["excludeDomains"] = self._filters(spec["blocked_domains"])
        if spec.get("country"):
            body["userLocation"] = spec["country"]
        return body

    def search(self, query, spec):
        request = urllib.request.Request(
            self.endpoint + "/search", data=json.dumps(self.body(query, spec)).encode("utf-8"), method="POST",
            headers={"content-type": "application/json", "x-api-key": self.key, "user-agent": "retool-relay/2"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise SearchError(_status_code(error.code), "HTTP %d%s" % (error.code, _tag(error))) from None
        except TimeoutError:
            raise SearchError("unavailable", "timeout") from None
        except (urllib.error.URLError, OSError) as error:
            raise SearchError("unavailable", type(error).__name__) from None
        except (ValueError, UnicodeError):
            raise SearchError("unavailable", "the answer is not JSON") from None
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SearchError("unavailable", "no results list")
        results = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            passages = row.get("highlights") if isinstance(row.get("highlights"), list) else []
            text = " ".join(str(item).strip() for item in passages if str(item).strip()) or str(row.get("text") or "")
            results.append(Result(str(row.get("title") or ""), row["url"], _page_age(row.get("publishedDate")),
                                  " ".join(text.split())[:SNIPPET_LIMIT]))
        return results


def _status_code(status):
    """Anthropic's error_code for a provider's HTTP status."""
    return {400: "invalid_tool_input", 422: "invalid_tool_input", 429: "too_many_requests"}.get(status, "unavailable")


def _tag(error):
    """Exa's machine-readable tag of a refusal, for the journal; never the body, which could name the query."""
    try:
        tag = json.loads(error.read(4096).decode("utf-8")).get("tag")
    except (ValueError, UnicodeError, AttributeError, OSError):
        return ""
    return " " + tag if isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", tag) else ""


def key_of(cfg):
    """The provider's key from the environment variable the config names; None when it is not set."""
    return os.environ.get(str(cfg.get("web_search_key_env") or "")) or None


def provider(cfg):
    """The search provider a config runs, or None: nothing is configured, or its key is not in the environment.

    A test (or an embedder) can put its own provider in cfg["_web_search"]: any object with search(query, spec)."""
    if cfg.get("_web_search") is not None:
        return cfg["_web_search"]
    if cfg.get("web_search_provider") != "exa" or key_of(cfg) is None:
        return None
    return Exa(key_of(cfg), cfg.get("web_search_endpoint") or "https://api.exa.ai",
               int(cfg.get("web_search_results") or 5), float(cfg.get("web_search_timeout") or 20))
