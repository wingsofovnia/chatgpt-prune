#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13"]
# ///
"""Bulk delete ChatGPT chats through the chatgpt.com backend API.

Delete is permanent. Run with --dry-run first.
"""
import argparse
import base64
import heapq
import json
import logging
import os
import random
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.spinner import SPINNERS
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

BASE = "https://chatgpt.com/backend-api"
WEB = "https://chatgpt.com"  # Links in tables. /c/{id} also opens project chats.
# Cloudflare returns 403 without a browser User-Agent.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
PAGE = 100  # API maximum: larger values get HTTP 422.
WORDS = ["REMOVE", "ERASE", "PURGE", "DISCARD", "DELETE"]
log = logging.getLogger("chatgpt_prune")
# UI (spinner, logs, excluded table, prompts) goes to stderr.
# stdout gets only the list to delete, so `> file` or a pipe captures just that.
# Not a terminal (file, pipe): rich falls back to 80 columns and cuts titles. Use a wide width there.
# emoji=False: a title with ':x:' must show as typed, not as an emoji.
err = Console(stderr=True, emoji=False, width=None if sys.stderr.isatty() else 250)
out = Console(emoji=False, width=None if sys.stdout.isatty() else 250)
# Built-in emoji frames end in a space, and the status line adds one more. Add trimmed copies.
for name in ("clock", "runner"):
    SPINNERS[f"{name}_trim"] = {**SPINNERS[name], "frames": [f.strip() for f in SPINNERS[name]["frames"]]}
FIELDS = ["id", "title", "create_time", "update_time", "pinned_time", "is_archived", "gizmo_id", "project"]


class AuthError(Exception):
    pass


def parse_duration(s):
    """'500ms' -> 0.5, '2s' -> 2.0, '1m' -> 60.0, '1.5' -> 1.5 (seconds)."""
    for suffix, scale in (("ms", 0.001), ("s", 1), ("m", 60)):
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * scale
    return float(s)


def parse_date(s):
    """ISO date or datetime. No zone means UTC. A plain date means 00:00 of that day."""
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def token_expiry(token):
    """Return the token's exp time, or None. Reads the JWT payload. Does not verify it."""
    try:
        p = token.split(".")[1]
        return datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))["exp"], timezone.utc)
    except Exception:
        return None


def countdown(show, text, seconds):
    """Sleep for seconds. Call show() with the time left, once per second."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        show(f"[yellow]{escape(text)} in {left:.0f}s…[/]")
        time.sleep(min(1.0, left))


class Api:
    on_wait_default = staticmethod(lambda text, seconds: time.sleep(seconds))

    def __init__(self, auth, delay=0.5, retries=4, retry_delay=10.0):
        self.auth = f"Bearer {auth}"
        self.delay, self.retries, self.retry_delay = delay, retries, retry_delay
        self.last = 0.0
        self.requests = self.retried = 0  # For the summary line.
        self.on_wait = Api.on_wait_default  # The UI replaces it to show a countdown.

    def call(self, method, path, body=None):
        url = f"{BASE}/{path}"
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": self.auth, "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
        for attempt in range(self.retries + 1):
            self.requests += 1
            time.sleep(max(0.0, self.last + self.delay - time.monotonic()))
            self.last = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=30) as r:
                    body = json.load(r)
                log.debug("%s %s -> %s (%.0f ms)", method, path, r.status, (time.monotonic() - self.last) * 1000)
                return body
            except urllib.error.HTTPError as e:
                log.debug("%s %s -> %s", method, path, e.code)
                if e.code == 401:
                    raise AuthError("HTTP 401: token is expired or not valid. Get a new token.") from None
                if (e.code != 429 and e.code < 500) or attempt == self.retries:
                    raise
                why = "rate limited (HTTP 429)" if e.code == 429 else f"HTTP {e.code}"
                # ChatGPT sends no Retry-After today (checked 2026-10). Kept in case Cloudflare or the API add it.
                retry_after = e.headers.get("Retry-After") or ""
                wait = float(retry_after) if retry_after.isdigit() else self.retry_delay * 2**attempt
            except OSError as e:  # URLError, timeouts, connection resets
                log.debug("%s %s -> %s", method, path, getattr(e, "reason", e))
                why = f"network error ({getattr(e, 'reason', e)})"
                if attempt == self.retries:
                    raise
                wait = self.retry_delay * 2**attempt
            log.debug("%s, retry %d/%d in %.1fs", why, attempt + 1, self.retries, wait)
            self.retried += 1
            self.on_wait(f"{why[0].upper()}{why[1:]}, retry {attempt + 1}/{self.retries}", wait)


def list_url(archived, offset, limit):
    """One URL for page reads and seek probes, so both see the same list."""
    # order=created returns HTTP 500 (checked 2026-10). No known client uses it.
    return f"conversations?offset={offset}&limit={limit}&order=updated&is_archived={archived}"


def paged_offset(api, archived, before):
    """Yield chats from the main list or the archive, newest first by update time. With --updated-before, skip ahead first."""
    offset = seek(api, archived, before) if before else 0
    while True:
        items = api.call("GET", list_url(archived, offset, PAGE)).get("items") or []
        yield from items
        if len(items) < PAGE:
            return
        offset += len(items)


def seek(api, archived, before):
    """Return the offset where chats older than before start, minus a margin. Reads 1 chat per request.

    The list is newest first. Exponential search for an upper bound, then binary search
    until the range fits in one page: reading that page costs less than more probes.
    """
    def older(k):
        items = api.call("GET", list_url(archived, k, 1)).get("items") or []
        return not items or updated(items[0]) < before

    lo, hi = 0, PAGE
    while hi < 10**7 and not older(hi):
        lo, hi = hi + 1, hi * 2
    while hi - lo > PAGE:
        mid = (lo + hi) // 2
        if older(mid):
            hi = mid
        else:
            lo = mid + 1
    log.debug("%s: skip to offset %d", "archive" if archived == "true" else "main list", max(0, lo - 10))
    # Margin: a chat deleted or archived elsewhere during the search moves older chats up. An update is safe.
    return max(0, lo - 10)


def ordered(chats, a):
    """Yield chats. Fail if the chats read are not newest first by update time, when the order matters.

    Early stop, seek and --limit need this order. Chats that seek skips are not read, so not checked.
    """
    if not (a.updated_after or a.updated_before or a.limit):
        yield from chats
        return
    last = None
    for c in chats:
        key = updated(c)
        if last is not None and key > last:
            raise RuntimeError(f"the chat list is not sorted by update time (at chat {c['id']})")
        last = key
        yield c


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


def updated(c):
    return parse_date(c.get("update_time") or c["create_time"])


def list_chats(api, a):
    """Yield chats from the main list, the archive and each project. Newest first by update time, no duplicates.

    Pages are read only when needed. Each source is newest first, so a merge keeps that order.
    With --updated-before, the main list and the archive skip ahead to it.
    """
    # Excluded sources are not read: nothing from them can be deleted.
    sources = [paged_offset(api, "false", a.updated_before)]
    if not a.exclude_archived:
        sources.append(paged_offset(api, "true", a.updated_before))
    # The main list does not include all project chats. Read each project too.
    projects = {} if a.exclude_project_chats else list_projects(api)
    wanted = {pid: name for pid, name in projects.items() if not a.project_name or name in a.project_name}
    log.debug("sources: main list%s, %d of %d projects %s", "" if a.exclude_archived else ", archive",
              len(wanted), len(projects), sorted(wanted.values()))
    for pid in wanted:
        sources.append(paged_cursor(api, f"gizmos/{pid}/conversations", cursor=0))  # By update time.
    seen = set()
    # The merge keeps each source's order, so one check on its output catches any unsorted source.
    for c in ordered(heapq.merge(*sources, key=updated, reverse=True), a):
        if a.updated_after and updated(c) < a.updated_after:
            log.debug("stop reading: next chat is older than --updated-after")
            return  # All next chats are older.
        if c["id"] not in seen:
            seen.add(c["id"])
            c["project"] = projects.get(c.get("gizmo_id"))
            yield c


def find_chats(api, a, progress=lambda read, to_delete, excluded: None):
    """Return (to_delete, excluded, stopped), newest first by update time. excluded is [(chat, reason)].

    Stop reading pages after --limit chats to delete. stopped is True if older chats exist that are not checked.
    """
    to_delete, excluded, read, stopped = [], [], 0, False
    chats = list_chats(api, a)
    for c in chats:
        read += 1
        if selected(c, a):
            reason = excluded_by(c, a)
            if reason:
                excluded.append((c, reason))
            else:
                to_delete.append(c)
        progress(read, len(to_delete), len(excluded))
        if len(to_delete) == a.limit:
            try:
                stopped = next(chats, None) is not None
            except OSError:  # Network error: the list is complete, so do not abort. A sort error must still fail.
                stopped = True
            break
    log.debug("checked %d chats: %d to delete, %d excluded%s", read, len(to_delete), len(excluded),
              ", stopped at --limit" if stopped else "")
    return to_delete, excluded, stopped


def selected(c, a):
    """Stage 1, select: time window, project and title. Dropped chats are not listed."""
    title = (c.get("title") or "").lower()
    return all([
        a.updated_after is None or updated(c) >= a.updated_after,
        a.updated_before is None or updated(c) < a.updated_before,
        not a.project_name or c.get("project") in a.project_name,
        not a.title_contains or any(t.lower() in title for t in a.title_contains),
    ])


def excluded_by(c, a):
    """Stage 2, filter: return why the chat is excluded, or None to delete it."""
    title = (c.get("title") or "").lower()
    project = c.get("project")
    in_project = (c.get("gizmo_id") or "").startswith("g-p-")
    if a.exclude_pinned and c.get("pinned_time"):
        return "pinned"
    if a.exclude_project_chats and in_project:
        return "project chat"
    if project in a.exclude_project_name:
        return "excluded project"
    # A project with no known name (e.g. shared) can be an excluded one. Keep its chats.
    if a.exclude_project_name and in_project and project is None:
        return "unknown project"
    for t in a.exclude_title_contains:
        if t.lower() in title:
            return f"title has {t!r}"
    return None


def render(chats, fmt):
    """Plain text for --output ids and json."""
    if fmt == "ids":
        return "\n".join(c["id"] for c in chats)
    return json.dumps([{k: c.get(k) for k in FIELDS} for c in chats], indent=2, ensure_ascii=False)


def table(chats, title, reasons=None, color="red", width=250):
    """Rich table. reasons: one string per chat. Adds a dimmed Reason column and a count per reason.

    Project, Pinned (📌) and Archived (📦) are hidden when no row has a value.
    Column widths are set here, so rich does not shrink columns. If the table does not fit in width,
    Created, Project, Updated, Archived and Pinned are hidden in that order. Title and Reason always stay.
    No ID column: the title links to the chat. --output ids and json give the IDs.
    """
    project_w = min(20, max((cell_len(clean(c.get("project") or "")) for c in chats), default=0))
    cols = [("Created", 10), ("Updated", 10)]  # (name, width)
    if any(c.get("pinned_time") for c in chats):
        cols.append(("Pinned", 6))
    if any(c.get("is_archived") for c in chats):
        cols.append(("Archived", 8))
    if project_w:
        cols.append(("Project", max(project_w, 7)))
    if reasons:
        cols.append(("Reason", min(20, max(6, *map(cell_len, reasons)))))
    border = lambda n: 3 * n + 1  # Each column adds 2 padding + 1 border line.
    for drop in ("Created", "Project", "Updated", "Archived", "Pinned"):
        if sum(w for _, w in cols) + border(len(cols) + 1) + 20 <= width:
            break
        cols = [col for col in cols if col[0] != drop]
    free = width - sum(w for _, w in cols) - border(len(cols) + 1)
    if free < 20 and reasons:  # Still too wide: Reason gives up space first, down to 8 cells.
        r_w = dict(cols)["Reason"]
        cols = [(n, max(8, r_w - (20 - free)) if n == "Reason" else w) for n, w in cols]
        free = width - sum(w for _, w in cols) - border(len(cols) + 1)
    cols.insert(0, ("Title", max(12, min(free, max((cell_len(chat_title(c)) for c in chats), default=5)))))

    cell = {
        "Created": lambda c, r: c["create_time"][:10],
        "Updated": lambda c, r: updated_day(c),
        "Pinned": lambda c, r: "📌" if c.get("pinned_time") else "",
        "Archived": lambda c, r: "📦" if c.get("is_archived") else "",
        "Project": lambda c, r: link(c["project"], f"{WEB}/g/{c['gizmo_id']}/project") if c.get("project") else "",
        "Reason": lambda c, r: Text(r),
        "Title": lambda c, r: chat_link(c),
    }
    # Title and Reason wrap inside their width. All other columns keep one line.
    options = {"Project": {"no_wrap": True, "overflow": "ellipsis"}, "Reason": {"style": "yellow"}, "Title": {},
               "Pinned": {"no_wrap": True, "justify": "center"}, "Archived": {"no_wrap": True, "justify": "center"}}
    caption = " · ".join(f"{escape(r)}: {n}" for r, n in Counter(reasons).most_common()) if reasons else None
    t = Table(title=title, title_justify="left", title_style=f"bold {color}", border_style=color, box=box.ROUNDED,
              caption=caption, caption_justify="left", row_styles=["dim"] if reasons else None)
    for name, w in cols:
        t.add_column(name, width=w, **options.get(name, {"no_wrap": True}))
    for i, c in enumerate(chats):
        t.add_row(*(cell[name](c, reasons[i] if reasons else None) for name, _ in cols))
    return t


def clean(text):
    """One line: line breaks would split a table row or a prompt. Control chars (ESC) would reach the terminal.

    Lone surrogates (Cs) crash the stdout encoder. Bidi overrides would reorder the prompt after the title.
    Other Cf stays: ZWJ joins emoji, ZWNJ shapes Persian.
    """
    def keep(ch):
        cat = unicodedata.category(ch)
        return " " if cat == "Cc" else "" if cat == "Cs" or "\u202a" <= ch <= "\u202e" or "\u2066" <= ch <= "\u2069" else ch
    return " ".join("".join(map(keep, text)).split())


def link(text, url):
    """Text, not markup: a title like 'a\\\\' or '[/]' must not break the link tags."""
    return Text(clean(text), style=f"link {url}")


def chat_title(c):
    """Empty: a placeholder, so the link has text."""
    return clean(c.get("title") or "") or "(no title)"


def chat_link(c):
    return link(chat_title(c), f"{WEB}/c/{c['id']}")


def updated_day(c):
    """String slice, not parse_date: a display string must never raise."""
    return (c.get("update_time") or c["create_time"])[:10]


def ask(prompt):
    """prompt is rich markup or Text. Shown on stderr, so `> file` gets only the list."""
    return err.input(prompt, emoji=False).strip()


def pick(chats):
    """Ask for each chat. y = delete, n = keep, a = delete this and all next, q = stop asking."""
    picked = []
    for i, c in enumerate(chats):
        prompt = Text.assemble(f"[{i + 1}/{len(chats)}] Delete ", chat_link(c), f" ({updated_day(c)})? [y/N/a/q] ")
        ans = ask(prompt).lower()
        if not ans and err.is_terminal and not err.legacy_windows:
            # Enter leaves the answer blank in scrollback. Write N after the prompt: up 1 line, to its end, back down.
            # Measure the last line as rich wrapped it (word wrap, wide chars), not by modulo.
            last = Segment.get_line_length(err.render_lines(prompt, pad=False)[-1])
            if last < err.width:  # A full last line has no room for N.
                err.file.write(f"\x1b[1A\x1b[{last + 1}GN\x1b[1E")
                err.file.flush()
        if ans == "q":
            break
        if ans == "a":
            return picked + chats[i:]
        if ans == "y":
            picked.append(c)
    return picked


def delete_all(api, chats, continue_on_error=False):
    """Return (deleted, failed). Without continue_on_error, stop after the first failure.

    Print a summary at the end, also after an error or Ctrl-C.
    """
    deleted, failed = [], []
    bar = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TaskProgressColumn(),
                   TimeRemainingColumn(), console=err)
    try:
        with bar:
            task = bar.add_task("Deleting", total=len(chats))

            def waiting(text, seconds):
                countdown(lambda t: bar.update(task, description=f"⏳ {t}"), text, seconds)
                bar.update(task, description="🏃 [yellow]Retrying…[/]")

            api.on_wait = waiting
            done = lambda: bar.update(task, advance=1, description="Deleting")
            for c in chats:
                try:
                    r = api.call("PATCH", f"conversation/{c['id']}", {"is_visible": False})
                    if not r.get("success"):
                        raise RuntimeError(f"unexpected response: {r}")
                except AuthError:
                    raise
                except Exception as e:
                    failed.append(c)
                    done()
                    err.print(Text.assemble(("✖ failed ", "red"), chat_link(c), f" ({c['id']}): {e}"))
                    if not continue_on_error:
                        break
                else:
                    deleted.append(c)
                    done()
                    err.print(Text.assemble(("✔ ", "green"), "deleted ", chat_link(c), f" ({updated_day(c)})"))
    finally:
        api.on_wait = Api.on_wait_default
        err.print(f"[green]✔ deleted {len(deleted)}[/] · [red]✖ failed {len(failed)}[/] · "
                  f"skipped {len(chats) - len(deleted) - len(failed)}")
    return deleted, failed


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    flag = dict(action="store_true")
    p.add_argument("--authorization", help="plain JWT (eyJ...), no 'Bearer '. Default: env CHATGPT_ACCESS_TOKEN. The env is safer: argv shows in ps and shell history.")
    g = p.add_argument_group("time filters (ISO date or datetime, UTC if no zone; before excludes it, after includes it)")
    g.add_argument("--updated-before", type=parse_date)
    g.add_argument("--updated-after", type=parse_date)
    g = p.add_argument_group("chat state")
    g.add_argument("--exclude-pinned", **flag)
    g.add_argument("--exclude-project-chats", **flag)
    g.add_argument("--exclude-archived", **flag)
    g = p.add_argument_group("projects (exact name as shown in the UI, repeatable)")
    g.add_argument("--project-name", action="append", default=[])
    g.add_argument("--exclude-project-name", action="append", default=[])
    g = p.add_argument_group("title (case-insensitive, repeatable, a chat matches if any value matches)")
    g.add_argument("--title-contains", action="append", default=[])
    g.add_argument("--exclude-title-contains", action="append", default=[])
    g = p.add_argument_group("safety")
    g.add_argument("--dry-run", **flag, help="list matches, delete nothing")
    g.add_argument("--limit", type=int, help="delete at most N chats, newest first")
    g.add_argument("--interactive", **flag, help="ask for each chat")
    g.add_argument("--yes", **flag, help="skip the typed confirmation")
    g = p.add_argument_group("output")
    g.add_argument("--output", choices=["table", "json", "ids"], default="table")
    g.add_argument("--verbose", **flag, help="debug log to stderr: requests, retries, token expiry, sources. Traceback on errors.")
    g = p.add_argument_group("execution")
    g.add_argument("--delay", type=parse_duration, default=0.5, help="min time between requests, e.g. 500ms, 2s (default 500ms)")
    g.add_argument("--retries", type=int, default=4, help="retries on 429, 5xx and network errors (default 4)")
    g.add_argument("--retry-delay", type=parse_duration, default=10.0, help="first retry wait, doubles each time (default 10s)")
    g.add_argument("--continue-on-error", **flag)
    a = p.parse_args(argv)
    if a.project_name and a.exclude_project_chats:
        p.error("--project-name and --exclude-project-chats conflict")
    if a.limit is not None and a.limit < 1:
        p.error("--limit must be 1 or more")
    if min(a.retries, a.delay, a.retry_delay) < 0:
        p.error("--retries, --delay and --retry-delay must be 0 or more")
    a.authorization = (a.authorization or os.environ.get("CHATGPT_ACCESS_TOKEN") or "").strip()
    if not a.authorization:
        p.error("give --authorization or set CHATGPT_ACCESS_TOKEN")
    # Smoke test only. The server rejects a bad token. Non-ASCII would crash before any request.
    if not a.authorization.isascii() or " " in a.authorization or a.authorization.count(".") != 2:
        p.error("not a plain JWT (eyJ..., no 'Bearer '). Is the clipboard holding something else?")
    return a


def main(argv=None):
    a = parse_args(argv)
    # No log output at all without --verbose.
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.CRITICAL + 1, format="%(message)s", force=True,
                        handlers=[RichHandler(console=err, show_path=False, log_time_format="%H:%M:%S")])
    exp = token_expiry(a.authorization)
    if exp and exp < datetime.now(timezone.utc):
        log.warning("token expired at %s. Get a new token.", exp.isoformat(timespec="minutes"))
    log.debug("token expires %s", exp.isoformat(timespec="minutes") if exp else "unknown (unreadable payload)")
    api = Api(a.authorization, a.delay, a.retries, a.retry_delay)
    try:
        checked, retrying, started = 0, False, time.monotonic()

        def reading(n, d, e):
            nonlocal checked, retrying
            checked = n
            text = f"Reading chats… checked {n} · to delete {d} · excluded {e}"
            # Back to dots after a retry. Only then: a new spinner restarts its animation.
            status.update(text, **({"spinner": "dots"} if retrying else {}))
            retrying = False

        with err.status("Reading chats…") as status:
            def waiting(text, seconds):
                nonlocal retrying
                status.update(spinner="clock_trim")  # Once: a new spinner restarts its animation.
                countdown(status.update, text, seconds)
                status.update("[yellow]Retrying…[/]", spinner="runner_trim")
                retrying = True

            api.on_wait = waiting
            try:
                chats, excluded, stopped = find_chats(api, a, reading)
            finally:
                api.on_wait = Api.on_wait_default
        err.print(f"[green]✔[/] Read {checked} chats in {time.monotonic() - started:.0f}s · "
                  f"{api.requests} requests · {api.retried} retries")

        # Stage 2 result first, so the list to delete is next to the prompt.
        if excluded:
            cs, rs = zip(*excluded)
            err.print(table(cs, f"🛡  Excluded, not deleted ({len(excluded)})", rs, color="yellow", width=err.width))
        if a.exclude_archived:
            err.print("[dim]Archive not read (--exclude-archived).[/]")
        if a.exclude_project_chats:
            err.print("[dim]Project chat lists not read (--exclude-project-chats). Only project chats in the main list are shown.[/]")
        err.print()  # Blank line between the excluded and the to-delete lists.
        if a.output == "table":
            out.print(table(chats, f"🗑  To delete ({len(chats)})", width=out.width))
        else:
            err.print(f"To delete ({len(chats)}):")
            print(render(chats, a.output), flush=True)
        cutoff = ". Stopped at --limit: older chats not checked" if stopped else ""
        err.print(f"[bold]{len(chats)} to delete · {len(excluded)} excluded[/]{cutoff}")

        if a.dry_run or not chats:
            err.print("[dim]Dry run: nothing deleted.[/]" if a.dry_run else "Nothing to delete.")
            return 0
        if (a.interactive or not a.yes) and not sys.stdin.isatty():
            err.print("[red]error:[/] confirmation needs a terminal. Use --yes to skip it.")
            return 2
        if a.interactive:
            chats = pick(chats)
            if not chats:
                err.print("No chats picked: nothing deleted.")
                return 0
        if not a.yes:
            word = random.choice(WORDS)
            if ask(f"Delete {len(chats)} chats permanently? Type [bold red]{word}[/] to confirm: ") != word:
                err.print("Aborted: nothing deleted.")
                return 1

        _, failed = delete_all(api, chats, a.continue_on_error)
        return 1 if failed else 0
    except (KeyboardInterrupt, EOFError):
        err.print("\nAborted.")
        return 1
    except Exception as e:
        if a.verbose:
            raise
        err.print(f"[red]error:[/] {escape(str(e))}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
