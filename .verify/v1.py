
import sys
sys.path.insert(0, r"C:\specialinay_papka")
import auth, redact, apierr, executor, config
AL = config.DEFAULTS["exec_allowlist"]
out = []
try:
    auth.verify({"authorization": "Bearer ключ-кириллица"}, "", "test-key-123"); out.append("1 nonascii: 401 ok (no crash)")
except apierr.ApiError as e: out.append("1 nonascii: ApiError %s" % e.status)
except Exception as e: out.append("1 nonascii: CRASH %s: %s" % (type(e).__name__, e))
out.append("2 redact path: %r" % redact.redact("/v1/chat/completions?key=relay-SECRET1234567890abcdef"))
code, o = executor.run_command("type C:\\Windows\\win.ini", r"C:\specialinay_papka", policy="allowlist", allowlist=AL, limit=80)
out.append("3 outside-workspace type: code=%s out=%r" % (code, o[:40]))
for label, cmd in [("3b powershell arbitrary", 'powershell -c "Get-Content C:\\Users\\Redmi\\.ssh\\id_rsa"'),
                   ("4 allowlisted head + arbitrary tail", "git status && powershell -c remove-item x")]:
    try:
        executor.check_policy(cmd, "allowlist", AL); out.append("%s: ALLOWED" % label)
    except executor.PolicyError as e: out.append("%s: %s" % (label, e))
import validate
cfg = dict(config.DEFAULTS)
cases = [("empty content", {"model":"claude-opus-5-5","messages":[{"role":"user","content":""}]}),
 ("stream str", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"stream":"yes"}),
 ("budget bool", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"thinking":{"type":"enabled","budget_tokens":True}}),
 ("budget str", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"thinking":{"type":"enabled","budget_tokens":"4096"}}),
 ("effort upper", {"model":"claude-opus-5-5","messages":[{"role":"user","content":"x"}],"reasoning_effort":"HIGH"}),
 ("openai effort max", {"model":"gpt-5.2","messages":[{"role":"user","content":"x"}],"reasoning_effort":"max"}),
 ("thinking type off", {"model":"gpt-5.2","messages":[{"role":"user","content":"x"}],"thinking":{"type":"off"}})]
for label, payload in cases:
    try:
        p = validate.parse_chat(payload, cfg); out.append("5 %s -> stream=%r effort=%r" % (label, p.stream, p.effort))
    except apierr.ApiError as e: out.append("5 %s -> %s %s" % (label, e.status, e.message))
try:
    p2 = validate.parse_anthropic({"model":"claude-opus-5-5","max_tokens":10,"system":[{"type":"text","text":"S","cache_control":{"type":"ephemeral"}}],"messages":[{"role":"user","content":"hi"}]}, cfg)
    out.append("6 anthropic system blocks: %r" % p2.system)
except Exception as e: out.append("6 anthropic system blocks: %s %s" % (type(e).__name__, e))
print("\n".join(out))
