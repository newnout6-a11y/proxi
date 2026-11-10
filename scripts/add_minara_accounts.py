"""Add Minara accounts to config.json from a file of Bearer tokens.

Usage (PowerShell):
  python scripts/add_minara_accounts.py --tokens-file tokens.txt            # dry run: what would be added
  python scripts/add_minara_accounts.py --tokens-file tokens.txt --check    # ... and each new token's credits
  python scripts/add_minara_accounts.py --tokens-file tokens.txt --write    # add them to config.json

The file is read line by line: a line that starts with eyJ is a token (a JWT from Minara's web app), anything else
(names, notes, blank lines) is ignored. A token config.json already has, or another token of the same Minara user (the
JWT's `sub`), is skipped, and so is an expired one; --check asks GET /auth/me for every new token, shows its credits
and skips a token Minara answers 401/403 to. New accounts are labelled "Minara N" after the largest N in config.json and
have no model list (they serve every published model). --write saves them in one atomic write under the lock the relay
itself takes for config.json; the running relay serves them after a restart. Tokens are never printed: the report
names a token by its line in the file.
"""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import accounts
import apierr
import config
import minara_accounts


class Refused(Exception):
    """What stops the script before anything is written; the message is safe to print."""


def read_tokens(path):
    """(line number, token) for every line of the file that starts with eyJ."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise Refused("cannot read the tokens file %s (%s)" % (path, type(error).__name__)) from None
    return [(number, line.strip()) for number, line in enumerate(text.splitlines(), 1)
            if line.strip().startswith("eyJ")]


def read_config(path):
    """config.json as saved, and its account records; a file without an accounts list is refused."""
    try:
        with open(path, encoding="utf-8") as source:
            saved = json.load(source)
    except (OSError, ValueError) as error:
        raise Refused("cannot read %s (%s)" % (path, type(error).__name__)) from None
    if not isinstance(saved, dict) or not isinstance(saved.get("accounts"), list):
        raise Refused("%s has no accounts list: add the first account in the admin panel (Аккаунты → Добавить "
                      "аккаунт), which turns a single-account file into a pool" % path)
    return saved, [record for record in saved["accounts"] if isinstance(record, dict)]


def check(token, base=minara_accounts.DEFAULT_BASE):
    """What GET /auth/me says of the token: ("ok", balance info), ("dead", "HTTP 401") or ("error", reason)."""
    info = accounts.MinaraAccount({"bearer_token": token, "base": base}).get_balance()
    error = info.get("error")
    if error == "upstream_auth_error":
        return "dead", "Minara refused the token (HTTP 401/403)"
    if error:
        return "error", error
    return "ok", info


def _day(epoch):
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return "%d" % epoch


def describe(result, line, now, probe=None, line_of=None):
    """One line of the report: what happens to the token on this line of the file. Never the token.

    `line_of` turns a position in the planned token list into its line in the file (a repeated token names its first)."""
    status = result["status"]
    if status == "added":
        record = result["record"]
        expiry = result.get("expires_at")
        text = "add        %-12s %s" % (record["label"], "expires %s (%.1f days)" % (_day(expiry), (expiry - now) / 86400)
                                       if expiry is not None else "expiry unknown (not a readable JWT)")
        if probe is not None and probe[0] == "ok":
            info = probe[1]
            text += "   credits %.2f ($%.2f)" % (info["available"], info["usd"])
        elif probe is not None:
            text += "   balance unknown: %s" % probe[1]
    elif status == "renewed":
        other = result["of"].get("label") or accounts.cfg_tag(result["of"], accounts.UNNAMED)
        was = result.get("was_expires_at")
        text = "renew      %-12s new token expires %s%s" % (other, _day(result["expires_at"]),
                                                          ", the stored one %s" % _day(was) if was else "")
    elif status == "duplicate" and "repeat_of" in result and line_of is not None:
        text = "duplicate  %s the token on line %d" % (
            "repeats" if result["reason"] == "same_token" else "same Minara user as", line_of(result["repeat_of"]))
    elif status == "duplicate":
        other = result["of"].get("label") or accounts.cfg_tag(result["of"], accounts.UNNAMED)
        text = ("duplicate  %s already has this token" % other if result["reason"] == "same_token"
                else "duplicate  %s belongs to the same Minara user%s" % (
                    other, " (this token expires later, but the account reads its token from an environment variable)"
                    if result.get("newer") else ""))
    elif status == "expired":
        text = "expired    on %s" % _day(result["expires_at"])
    elif status == "dead":
        text = "dead       %s" % result["error"]
    else:
        text = "invalid    %s" % result["error"]
    return "line %-4d %s" % (line, text)


def run(tokens_file, config_path, *, write=False, probe=False, out=print):
    """The whole run; returns the exit code. Tokens never reach `out`."""
    lines = read_tokens(tokens_file)
    if not lines:
        raise Refused("no token in %s: a token is a line that starts with eyJ" % tokens_file)
    _, records = read_config(config_path)
    now = time.time()
    try:
        results, _ = minara_accounts.plan(records, [token for _, token in lines], now=now)
    except apierr.ApiError as error:
        raise Refused(error.message) from None
    probes = {}  # line -> what GET /auth/me said of the token on it
    if probe:
        for result in results:
            if result["status"] == "added":
                probes[lines[result["token"] - 1][0]] = check(result["record"]["bearer_token"])
    dead = {line: outcome[1] for line, outcome in probes.items() if outcome[0] == "dead"}
    minara = sum(1 for record in records if record.get("upstream") == "minara")
    out("%s: %d accounts (%d Minara); %d tokens in %s" % (config_path, len(records), minara, len(lines), tokens_file))
    if not write:
        for result in results:
            line = lines[result["token"] - 1][0]
            shown = {"status": "dead", "error": dead[line]} if line in dead else result
            out(describe(shown, line, now, probes.get(line), lambda position: lines[position - 1][0]))
        count = sum(1 for result in results if result["status"] == "added") - len(dead)
        renew = sum(1 for result in results if result["status"] == "renewed")
        out("Dry run: %d account%s would be added%s. Run again with --write to save %s."
            % (count, "" if count == 1 else "s", ", %d renewed" % renew if renew else "",
               "it" if count + renew == 1 else "them"))
        return 0
    # The file may have changed since it was read: plan again on what it holds now, under the lock the relay takes.
    kept = [(line, token) for line, token in lines if line not in dead]
    added = []
    report = [(line, describe({"status": "dead", "error": reason}, line, now)) for line, reason in dead.items()]
    if kept:
        with config.storage_lock(str(config_path)):
            saved, records = read_config(config_path)
            try:
                results, added = minara_accounts.plan(records, [token for _, token in kept], now=time.time())
            except apierr.ApiError as error:
                raise Refused(error.message) from None
            renewed = minara_accounts.apply_renewals(results)
            if added:
                saved["accounts"].extend(added)
                config._assign_account_ids(saved)
            if added or renewed:
                config._write_file(str(config_path), saved)
        for result in results:
            line = kept[result["token"] - 1][0]
            report.append((line, describe(result, line, now, probes.get(line), lambda position: kept[position - 1][0])))
    for _, text in sorted(report):
        out(text)
    renewals = sum(1 for result in results if result["status"] == "renewed") if kept else 0
    out("Added %d account%s%s to %s. Restart the relay to serve %s."
        % (len(added), "" if len(added) == 1 else "s", " and renewed %d" % renewals if renewals else "", config_path,
           "it" if len(added) + renewals == 1 else "them"))
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tokens-file", required=True, type=pathlib.Path,
                        help="text file; every line that starts with eyJ is a token, the rest is ignored")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(config.CONFIG_PATH),
                        help="the relay's config.json (default: the one beside the relay)")
    parser.add_argument("--write", action="store_true", help="save the new accounts (without it: a dry run)")
    parser.add_argument("--check", action="store_true",
                        help="ask GET /auth/me for each new token: show its credits, skip one Minara refuses")
    args = parser.parse_args(argv)
    try:
        return run(args.tokens_file, args.config, write=args.write, probe=args.check)
    except Refused as error:
        print("error: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
