#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bulk delete ChatGPT chats through the chatgpt.com backend API.

Delete is permanent. Run with --dry-run first.
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://chatgpt.com/backend-api"
# Cloudflare returns 403 without a browser User-Agent.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
PAGE = 28  # Larger pages get HTTP 429 fast.
WORDS = ["REMOVE", "ERASE", "PURGE", "DISCARD", "DELETE"]
FIELDS = ["id", "title", "create_time", "update_time", "pinned_time", "is_archived", "gizmo_id", "project"]


class AuthError(Exception):
    pass


def parse_duration(s):
    """'500ms' -> 0.5, '2s' -> 2.0, '1m' -> 60.0, '1.5' -> 1.5 (seconds)."""
    for suffix, scale in (("ms", 0.001), ("s", 1), ("m", 60)):
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * scale
    return float(s)


def parse_date(s, end=False):
    """ISO date or datetime. No zone means UTC. With end=True, a plain date means the end of that day."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end and len(s) == 10:
        dt += timedelta(days=1, microseconds=-1)
    return dt


class Api:
    def __init__(self, auth, delay=1.0, retries=3, retry_delay=2.0, verbose=False):
        self.auth = f"Bearer {auth}"
        self.delay, self.retries, self.retry_delay, self.verbose = delay, retries, retry_delay, verbose
        self.last = 0.0

    def _log(self, method, url, status):
        if self.verbose:
            print(f"{method} {url} -> {status}  [Authorization: Bearer ****]", file=sys.stderr)

    def call(self, method, path, body=None):
        url = f"{BASE}/{path}"
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": self.auth, "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
        for attempt in range(self.retries + 1):
            time.sleep(max(0.0, self.last + self.delay - time.monotonic()))
            self.last = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=30) as r:
                    self._log(method, url, r.status)
                    return json.load(r)
            except urllib.error.HTTPError as e:
                self._log(method, url, e.code)
                if e.code == 401:
                    raise AuthError("HTTP 401: token is expired or not valid. Get a new token.") from None
                if (e.code != 429 and e.code < 500) or attempt == self.retries:
                    raise
                retry_after = e.headers.get("Retry-After") or ""
                wait = float(retry_after) if retry_after.isdigit() else self.retry_delay * 2**attempt
            except urllib.error.URLError as e:
                self._log(method, url, e.reason)
                if attempt == self.retries:
                    raise
                wait = self.retry_delay * 2**attempt
            time.sleep(wait)


def paged_offset(api, archived):
    offset = 0
    while True:
        items = api.call("GET", f"conversations?offset={offset}&limit={PAGE}&order=updated&is_archived={archived}").get("items") or []
        yield from items
        if len(items) < PAGE:
            return
        offset += len(items)


def paged_cursor(api, path, cursor=None):
    while True:
        query = "" if cursor is None else f"{'&' if '?' in path else '?'}cursor={urllib.parse.quote(str(cursor))}"
        data = api.call("GET", path + query)
        yield from data.get("items") or []
        previous, cursor = cursor, data.get("cursor")
        if not cursor or cursor == previous:
            return


def list_projects(api):
    """Return {project_id: display name}."""
    items = paged_cursor(api, "gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=0")
    return {i["gizmo"]["gizmo"]["id"]: i["gizmo"]["gizmo"]["display"]["name"] for i in items}


def list_chats(api, a):
    """Collect chats from the main list, the archive and each project. Remove duplicates by id."""
    chats = {}

    def add(items):
        for c in items:
            chats.setdefault(c["id"], c)

    if a.archived != "only":
        add(paged_offset(api, "false"))
    if a.archived != "exclude":
        add(paged_offset(api, "true"))
    # The main list does not include all project chats. Read each project too.
    projects = {} if a.exclude_project_chats else list_projects(api)
    for pid, name in projects.items():
        if (not a.project_name or name in a.project_name) and name not in a.exclude_project_name:
            add(paged_cursor(api, f"gizmos/{pid}/conversations", cursor=0))
    for c in chats.values():
        c["project"] = projects.get(c.get("gizmo_id"))
    return list(chats.values())


def keep(c, a):
    created = parse_date(c["create_time"])
    updated = parse_date(c.get("update_time") or c["create_time"])
    title = (c.get("title") or "").lower()
    project = c.get("project")
    return all([
        a.create_time_from is None or created >= a.create_time_from,
        a.create_time_to is None or created <= a.create_time_to,
        a.update_time_from is None or updated >= a.update_time_from,
        a.update_time_to is None or updated <= a.update_time_to,
        not (a.exclude_pinned and c.get("pinned_time")),
        not (a.exclude_project_chats and (c.get("gizmo_id") or "").startswith("g-p-")),
        a.archived == "include" or bool(c.get("is_archived")) == (a.archived == "only"),
        not a.project_name or project in a.project_name,
        project not in a.exclude_project_name,
        not a.title_contains or any(t.lower() in title for t in a.title_contains),
        not any(t.lower() in title for t in a.exclude_title_contains),
    ])


def render(chats, fmt):
    if fmt == "ids":
        return "\n".join(c["id"] for c in chats)
    if fmt == "json":
        return json.dumps([{k: c.get(k) for k in FIELDS} for c in chats], indent=2, ensure_ascii=False)
    lines = [f"{'ID':36}  {'CREATED':10}  {'UPDATED':10}  {'PA':2}  {'PROJECT':20}  TITLE"]
    for c in chats:
        flags = ("P" if c.get("pinned_time") else "-") + ("A" if c.get("is_archived") else "-")
        lines.append(f"{c['id']:36}  {c['create_time'][:10]:10}  {(c.get('update_time') or '')[:10]:10}  {flags}  "
                     f"{(c.get('project') or '')[:20]:20}  {c.get('title') or ''}")
    return "\n".join(lines)


def ask(prompt):
    # Prompt on stderr, so `> file` gets only the list.
    print(prompt, end="", file=sys.stderr, flush=True)
    return input().strip()


def pick(chats):
    """Ask for each chat. y = delete, n = keep, a = delete this and all next, q = stop asking."""
    out = []
    for i, c in enumerate(chats):
        ans = ask(f"[{i + 1}/{len(chats)}] delete {c['id']} {c.get('title')!r}? [y/N/a/q] ").lower()
        if ans == "q":
            break
        if ans == "a":
            return out + chats[i:]
        if ans == "y":
            out.append(c)
    return out


def delete_all(api, chats, continue_on_error=False):
    """Return (deleted, failed). Without continue_on_error, stop after the first failure."""
    deleted, failed = [], []
    for c in chats:
        try:
            r = api.call("PATCH", f"conversation/{c['id']}", {"is_visible": False})
            if not r.get("success"):
                raise RuntimeError(f"unexpected response: {r}")
        except AuthError:
            raise
        except Exception as e:
            failed.append(c)
            print(f"failed {c['id']}: {e}", file=sys.stderr)
            if not continue_on_error:
                break
        else:
            deleted.append(c)
            print(f"deleted {c['id']}", file=sys.stderr)
    return deleted, failed


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    flag = dict(action="store_true")
    p.add_argument("--authorization", help="plain JWT (eyJ...), no 'Bearer '. Default: env OPENAI_API_KEY. The env is safer: argv shows in ps and shell history.")
    g = p.add_argument_group("time filters (ISO date or datetime, UTC if no zone; a plain date in -to includes that day)")
    g.add_argument("--create-time-from", type=parse_date)
    g.add_argument("--create-time-to", type=lambda s: parse_date(s, end=True))
    g.add_argument("--update-time-from", type=parse_date)
    g.add_argument("--update-time-to", type=lambda s: parse_date(s, end=True))
    g = p.add_argument_group("chat state")
    g.add_argument("--exclude-pinned", **flag)
    g.add_argument("--exclude-project-chats", **flag)
    g.add_argument("--archived", choices=["include", "exclude", "only"], default="include")
    g = p.add_argument_group("projects (exact name as shown in the UI, repeatable)")
    g.add_argument("--project-name", action="append", default=[])
    g.add_argument("--exclude-project-name", action="append", default=[])
    g = p.add_argument_group("title (case-insensitive, repeatable, a chat matches if any value matches)")
    g.add_argument("--title-contains", action="append", default=[])
    g.add_argument("--exclude-title-contains", action="append", default=[])
    g = p.add_argument_group("safety")
    g.add_argument("--dry-run", **flag, help="list matches, delete nothing")
    g.add_argument("--limit", type=int, help="delete at most N chats, most recently updated first")
    g.add_argument("--interactive", **flag, help="ask for each chat")
    g.add_argument("--yes", **flag, help="skip the typed confirmation")
    g = p.add_argument_group("output")
    g.add_argument("--output", choices=["table", "json", "ids"], default="table")
    g.add_argument("--verbose", **flag, help="log each request to stderr, token masked")
    g = p.add_argument_group("execution")
    g.add_argument("--delay", type=parse_duration, default=1.0, help="min time between requests, e.g. 500ms, 1s (default 1s)")
    g.add_argument("--retries", type=int, default=3, help="retries on 429, 5xx and network errors")
    g.add_argument("--retry-delay", type=parse_duration, default=2.0, help="first retry wait, doubles each time (default 2s)")
    g.add_argument("--continue-on-error", **flag)
    a = p.parse_args(argv)
    if a.project_name and a.exclude_project_chats:
        p.error("--project-name and --exclude-project-chats conflict")
    if a.limit is not None and a.limit < 1:
        p.error("--limit must be 1 or more")
    a.authorization = a.authorization or os.environ.get("OPENAI_API_KEY")
    if not a.authorization:
        p.error("give --authorization or set OPENAI_API_KEY")
    if a.authorization.lower().startswith("bearer "):
        p.error("give the plain JWT, without 'Bearer '")
    return a


def main(argv=None):
    a = parse_args(argv)
    api = Api(a.authorization, a.delay, a.retries, a.retry_delay, a.verbose)
    try:
        chats = [c for c in list_chats(api, a) if keep(c, a)]
        chats.sort(key=lambda c: c.get("update_time") or "", reverse=True)
        chats = chats[: a.limit]

        print(render(chats, a.output))
        print(f"{len(chats)} chats match", file=sys.stderr)

        if a.dry_run or not chats:
            print("dry run: nothing deleted" if a.dry_run else "nothing to delete", file=sys.stderr)
            return 0
        if (a.interactive or not a.yes) and not sys.stdin.isatty():
            print("error: confirmation needs a terminal. Use --yes to skip it.", file=sys.stderr)
            return 2
        if a.interactive:
            chats = pick(chats)
        if not a.yes:
            word = random.choice(WORDS)
            if ask(f"Delete {len(chats)} chats permanently? Type {word} to confirm: ") != word:
                print("aborted: nothing deleted", file=sys.stderr)
                return 1

        deleted, failed = delete_all(api, chats, a.continue_on_error)
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"deleted {len(deleted)}, failed {len(failed)}, skipped {len(chats) - len(deleted) - len(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
