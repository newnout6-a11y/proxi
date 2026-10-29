import json
import re
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def analyze_relay_log():
    log_path = r"c:\specialinay_papka\relay.log"
    if not os.path.exists(log_path):
        print(f"ERROR: {log_path} not found")
        return None

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    print(f"=== RELAY.LOG FORENSIC ANALYSIS ===")
    print(f"Total lines in relay.log: {len(lines)}")

    # Extract all request entries
    # Pattern: relay <req_id> POST /v1/messages <status> <model> <duration>ms
    # Also associate with preceding [body] and [cache] or [raw] usage

    requests = []
    current_body = None
    current_cache = None
    current_usage = None
    current_effort_log = None

    body_regex = re.compile(r"^\[body\]\s+(.*)$")
    cache_regex = re.compile(r"^\[cache\]\s+in=(\S+)\s+read=(\S+)\s+write=(\S+)")
    raw_usage_regex = re.compile(r'"promptTokensReadFromCache":(\d+)')
    raw_usage_write_regex = re.compile(r'"promptTokensWrittenToCache":(\d+)')
    effort_log_regex = re.compile(r"^\[effort\]\s+(.*)$")
    req_regex = re.compile(r"^relay\s+([a-f0-9]+)\s+POST\s+(\S+)\s+(\d+)\s+([\w\.\-]+)?\s*(\d+)ms")

    for line in lines:
        bm = body_regex.match(line)
        if bm:
            try:
                current_body = json.loads(bm.group(1))
            except Exception:
                current_body = bm.group(1)

        cm = cache_regex.match(line)
        if cm:
            current_cache = {
                "in": cm.group(1),
                "read": cm.group(2),
                "write": cm.group(3)
            }

        em = effort_log_regex.match(line)
        if em:
            current_effort_log = em.group(1)

        rm = req_regex.match(line)
        if rm:
            req_id = rm.group(1)
            path = rm.group(2)
            status = int(rm.group(3))
            model = rm.group(4)
            duration_ms = int(rm.group(5))

            body_effort = None
            body_model = None
            cache_key = None
            tools_count = None
            if isinstance(current_body, dict):
                body_effort = current_body.get("reasoningEffort")
                body_model = current_body.get("model")
                cache_key = current_body.get("openAICacheKey")
                tools_count = len(current_body.get("tools") or [])

            requests.append({
                "id": req_id,
                "path": path,
                "status": status,
                "model": model or body_model,
                "duration_ms": duration_ms,
                "body_effort": body_effort,
                "effort_log": current_effort_log,
                "cache": current_cache,
                "cache_key": cache_key,
                "tools_count": tools_count
            })
            current_body = None
            current_cache = None
            current_effort_log = None

    print(f"Total /v1/messages requests parsed: {len(requests)}")

    # Analyze durations and cache hits
    durations = [r["duration_ms"] for r in requests if r["path"] == "/v1/messages"]
    if durations:
        print(f"\nDuration statistics for /v1/messages:")
        print(f"  Count: {len(durations)}")
        print(f"  Min: {min(durations)}ms ({min(durations)/1000:.2f}s)")
        print(f"  Max: {max(durations)}ms ({max(durations)/1000:.2f}s)")
        print(f"  Avg: {sum(durations)/len(durations):.1f}ms ({sum(durations)/(len(durations)*1000):.2f}s)")

    # Cache hits breakdown
    cache_reads = []
    for r in requests:
        if r.get("cache") and r["cache"].get("read") not in (None, "None"):
            try:
                cache_reads.append(int(r["cache"]["read"]))
            except ValueError:
                pass

    print(f"\nCache hits statistics:")
    print(f"  Total requests with promptTokensReadFromCache > 0: {len(cache_reads)}")
    if cache_reads:
        print(f"  Min tokens read from cache: {min(cache_reads)}")
        print(f"  Max tokens read from cache: {max(cache_reads)}")
        print(f"  Avg tokens read from cache: {sum(cache_reads)/len(cache_reads):.1f}")

    # Reasoning effort breakdown
    efforts = {}
    for r in requests:
        eff = r["body_effort"]
        efforts[eff] = efforts.get(eff, 0) + 1

    print(f"\nReasoning effort distribution in upstream requests:")
    for eff, count in sorted(efforts.items(), key=lambda x: str(x[0])):
        print(f"  {eff}: {count}")

    # Inspect the most recent 40 requests (M2 verification period)
    print(f"\n--- Recent 30 requests (M2 Verification Period) ---")
    recent = requests[-30:]
    for r in recent:
        cache_str = f"read={r['cache']['read']}" if r.get('cache') else "no_cache"
        print(f"  Req {r['id']} | status={r['status']} | model={r['model']} | duration={r['duration_ms']}ms ({r['duration_ms']/1000:.2f}s) | effort={r['body_effort']} | {cache_str} | tools={r['tools_count']}")

    return requests

def analyze_claude_sessions():
    base_dir = r"C:\Users\Redmi\.claude\projects\C--specialinay-papka"
    sessions = [
        "e6b8a126-3ec5-4d4a-938d-87f022582e9b",
        "bd9930ab-ed46-405b-a465-23f263f322f8"
    ]
    print(f"\n=== CLAUDE SESSIONS FORENSIC ANALYSIS ===")
    for s_id in sessions:
        file_path = os.path.join(base_dir, f"{s_id}.jsonl")
        if not os.path.exists(file_path):
            print(f"ERROR: Session file {file_path} not found!")
            continue
        
        file_size = os.path.getsize(file_path)
        print(f"\nSession {s_id}:")
        print(f"  File size: {file_size:,} bytes")
        
        valid_lines = 0
        corrupt_lines = 0
        types = {}
        subagents = 0
        compacts = 0
        goals = 0

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    valid_lines += 1
                    t = obj.get("type", "unknown")
                    types[t] = types.get(t, 0) + 1

                    # Check for subagents, compact, goal mentions
                    line_str = line.lower()
                    if "subagent" in line_str or "cc_is_subagent" in line_str or '"agent"' in line_str:
                        subagents += 1
                    if "/compact" in line_str or "compact" in line_str:
                        compacts += 1
                    if "/goal" in line_str or "goal" in line_str:
                        goals += 1

                except Exception as ex:
                    corrupt_lines += 1
                    print(f"  Line {line_no} CORRUPT: {ex}")

        print(f"  Total valid JSON lines: {valid_lines}")
        print(f"  Total corrupt JSON lines: {corrupt_lines}")
        print(f"  Entry types: {types}")
        print(f"  Subagent references in session: {subagents}")
        print(f"  Compact references in session: {compacts}")
        print(f"  Goal references in session: {goals}")

if __name__ == "__main__":
    analyze_relay_log()
    analyze_claude_sessions()
