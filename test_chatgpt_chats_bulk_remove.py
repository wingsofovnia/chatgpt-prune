#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13"]  # Keep in sync with chatgpt_chats_bulk_remove.py.
# ///
"""Tests with a fake API. No network. Run: ./test_chatgpt_chats_bulk_remove.py -v"""
import base64
import io
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest import mock

from rich.console import Console

import chatgpt_chats_bulk_remove as m


def chat(id, title, created="2026-01-10T10:00:00.000000Z", updated=None, **kw):
    return {"id": id, "title": title, "create_time": created, "update_time": updated or created,
            "pinned_time": None, "is_archived": False, "gizmo_id": None, **kw}


ACTIVE = [chat("a1", "Plain chat"), chat("a2", "Pinned chat", pinned_time="2026-02-01T00:00:00Z"),
          chat("p1", "Project chat in main list", gizmo_id="g-p-1")]
ARCHIVED = [chat("r1", "Old archived", created="2025-05-01T00:00:00Z", is_archived=True)]
PROJECT = [chat("p1", "Project chat in main list", gizmo_id="g-p-1"), chat("p2", "Project only", gizmo_id="g-p-1")]


def page(path, items):
    """Slice items by the offset and limit in path, as the API does."""
    q = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
    start = int(q.get("offset", 0))
    return items[start:start + int(q.get("limit", m.PAGE))]


class FakeApi:
    requests = retried = 0

    def __init__(self, fail_ids=()):
        self.calls, self.fail_ids = [], set(fail_ids)

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "PATCH":
            if path.split("/")[1] in self.fail_ids:
                raise RuntimeError("boom")
            return {"success": True}
        if path.startswith("conversations?"):
            return {"items": page(path, ARCHIVED if "is_archived=true" in path else ACTIVE)}
        if path.startswith("gizmos/snorlax/sidebar"):
            return {"items": [{"gizmo": {"gizmo": {"id": "g-p-1", "display": {"name": "Work"}}}}], "cursor": None}
        if path.startswith("gizmos/g-p-1/conversations"):
            return {"items": PROJECT, "cursor": None}
        raise AssertionError(path)


def args(*argv):
    return m.parse_args(["--authorization", "h.p.s", *argv])


def select(*argv):
    """Return sorted ids to delete."""
    to_delete, _, _ = m.find_chats(FakeApi(), args(*argv))
    return sorted(c["id"] for c in to_delete)


def excluded(*argv):
    """Return {id: reason} of excluded chats."""
    _, out, _ = m.find_chats(FakeApi(), args(*argv))
    return {c["id"]: r for c, r in out}


class Parsing(unittest.TestCase):
    def test_dates(self):
        utc = timezone.utc
        self.assertEqual(m.parse_date("2026-10-29"), datetime(2026, 10, 29, tzinfo=utc))
        self.assertEqual(m.parse_date("2026-10-29T09:03:50.719809Z"), datetime(2026, 10, 29, 9, 3, 50, 719809, tzinfo=utc))

    def test_durations(self):
        self.assertEqual([m.parse_duration(s) for s in ("500ms", "2s", "1m", "1.5")], [0.5, 2.0, 60.0, 1.5])

    def test_rejects_negative_execution_values(self):
        for flag in ("--retries=-1", "--delay=-1s", "--retry-delay=-1"):
            with redirect_stderr(io.StringIO()):
                self.assertRaises(SystemExit, args, flag)

    def test_bool_flags(self):
        self.assertTrue(args("--exclude-pinned").exclude_pinned)
        self.assertFalse(args().exclude_pinned)

    def test_token_source(self):
        with mock.patch.dict(os.environ, {"CHATGPT_ACCESS_TOKEN": "e.n.v"}):
            self.assertEqual(m.parse_args(["--authorization", "f.l.ag"]).authorization, "f.l.ag")
            self.assertEqual(m.parse_args([]).authorization, "e.n.v")
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
            self.assertRaises(SystemExit, m.parse_args, [])
            self.assertRaises(SystemExit, m.parse_args, ["--authorization", "Bearer x"])
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertRaises(SystemExit, m.parse_args, ["--authorization", " Bearer h.p.s"])
            self.assertIn("no 'Bearer '", err.getvalue())
            self.assertRaises(SystemExit, m.parse_args, ["--authorization", "❯ ./chatgpt_chats_bulk_remove.py"])
            self.assertRaises(SystemExit, m.parse_args, ["--authorization", "токен.x.y"])
        self.assertEqual(m.Api("eyJ").auth, "Bearer eyJ")


class Filters(unittest.TestCase):
    def test_collects_all_sources_without_duplicates(self):
        self.assertEqual(select(), ["a1", "a2", "p1", "p2", "r1"])

    def test_project_name_is_set(self):
        a = args()
        self.assertEqual({c["id"]: c["project"] for c in m.list_chats(FakeApi(), a)}["p2"], "Work")

    def test_state_filters(self):
        self.assertEqual(select("--exclude-pinned"), ["a1", "p1", "p2", "r1"])
        self.assertEqual(select("--exclude-project-chats"), ["a1", "a2", "r1"])
        self.assertEqual(select("--exclude-archived"), ["a1", "a2", "p1", "p2"])

    def test_project_filters(self):
        self.assertEqual(select("--project-name", "Work"), ["p1", "p2"])
        self.assertEqual(select("--exclude-project-name", "Work"), ["a1", "a2", "r1"])

    def test_title_and_time(self):
        self.assertEqual(select("--title-contains", "PROJECT", "--exclude-title-contains", "only"), ["p1"])
        self.assertEqual(select("--created-before", "2025-05-02"), ["r1"])
        self.assertEqual(select("--created-after", "2026-01-10T10:00:00.000001Z"), [])

    def test_exclude_project_name_keeps_unknown_projects(self):
        shared = chat("s1", "Shared", gizmo_id="g-p-shared")
        a = args("--exclude-project-name", "Work")
        self.assertEqual(m.excluded_by({**shared, "project": None}, a), "unknown project")
        self.assertIsNone(m.excluded_by({**shared, "project": None}, args()))

    def test_lists_excluded_with_reason(self):
        self.assertEqual(excluded("--exclude-pinned"), {"a2": "pinned"})
        self.assertEqual(excluded("--exclude-project-chats"), {"p1": "project chat"})
        self.assertEqual(excluded("--exclude-title-contains", "only"), {"p2": "title has 'only'"})
        self.assertEqual(excluded("--exclude-project-name", "Work"), {"p1": "excluded project", "p2": "excluded project"})

    def test_excluded_sources_are_not_read(self):
        api = FakeApi()
        m.find_chats(api, args("--exclude-archived", "--exclude-project-chats"))
        self.assertFalse(any("is_archived=true" in p or p.startswith("gizmos/") for _, p in api.calls))

    def test_selection_drops_are_not_listed(self):
        self.assertEqual(excluded("--project-name", "Work", "--updated-before", "2026-01-01"), {})

    def test_title_contains_selects_silently(self):
        self.assertEqual(excluded("--title-contains", "plain"), {})


class PagedApi(FakeApi):
    """Main list: 2 full pages, chat c<i> created and updated i days before 2026-10-01. No archive, no projects."""

    def __init__(self, swap=None):
        super().__init__()
        day0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        when = lambda i: (day0 - timedelta(days=i)).isoformat()
        self.items = [chat(f"c{i}", f"c{i}", created=when(i), updated=when(i)) for i in range(2 * m.PAGE)]
        if swap:  # Put two chats out of order.
            i, j = swap
            self.items[i], self.items[j] = self.items[j], self.items[i]

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if "is_archived=false" in path:
            return {"items": page(path, self.items)}
        return {"items": [], "cursor": None}


class EarlyStop(unittest.TestCase):
    def test_limit_reads_only_needed_pages(self):
        api = PagedApi()
        self.assertEqual([c["id"] for c in m.find_chats(api, args("--limit", "3"))[0]], ["c0", "c1", "c2"])
        self.assertFalse(any(f"offset={m.PAGE}" in p for _, p in api.calls))

    def test_updated_after_stops_paging(self):
        api = PagedApi()
        self.assertEqual(len(m.find_chats(api, args("--updated-after", "2026-09-10"))[0]), 22)
        self.assertFalse(any(f"offset={m.PAGE}" in p for _, p in api.calls))

    def test_before_skips_newer_pages(self):
        for flag in ("--updated-before", "--created-before"):
            api = PagedApi()
            # c150 is 2026-05-04 00:00, the newest chat before the cutoff. c149 is 2026-05-05 00:00, not before it.
            to_delete = m.find_chats(api, args(flag, "2026-05-05"))[0]
            self.assertEqual([c["id"] for c in to_delete], [f"c{i}" for i in range(150, 200)], flag)
            self.assertNotIn(("GET", "conversations?offset=0&limit=100&order=" + ("updated" if "update" in flag else "created")
                              + "&is_archived=false"), api.calls, flag)

    def test_create_time_uses_created_order(self):
        api = PagedApi()
        m.find_chats(api, args("--created-after", "2026-09-01"))
        self.assertTrue(all("order=created" in p for _, p in api.calls if p.startswith("conversations?")))

    def test_unsorted_list_fails_when_order_matters(self):
        for swap in ((0, 1), (99, 100)):  # In one page, and across a page boundary.
            with self.assertRaisesRegex(RuntimeError, "not sorted by updated time"):
                m.find_chats(PagedApi(swap), args("--updated-after", "2020-01-01"))
        m.find_chats(PagedApi((0, 1)), args())  # No time filter, no --limit: order does not matter.
        with self.assertRaisesRegex(RuntimeError, "not sorted"):  # Also when the bad chat is the --limit peek.
            m.find_chats(PagedApi((2, 3)), args("--limit", "3"))

    def test_created_order_sorts_project_lists(self):
        class Api(FakeApi):
            def call(self, method, path, body=None):
                if path.startswith("gizmos/g-p-1/conversations"):  # Newest update first, not newest create.
                    return {"cursor": None, "items": [
                        chat("q1", "old", created="2026-01-01T00:00:00Z", updated="2026-06-01T00:00:00Z", gizmo_id="g-p-1"),
                        chat("q2", "new", created="2026-03-01T00:00:00Z", updated="2026-05-01T00:00:00Z", gizmo_id="g-p-1")]}
                return super().call(method, path, body)
        to_delete = m.find_chats(Api(), args("--created-after", "2026-02-01"))[0]
        self.assertEqual([c["id"] for c in to_delete], ["q2"])

    def test_seek_reads_few_probes_on_small_lists(self):
        api = PagedApi()
        m.find_chats(api, args("--exclude-archived", "--updated-before", "2026-05-05"))
        self.assertEqual(sum("limit=1&" in p for _, p in api.calls), 2)  # Offsets 100 and 200.

    def test_mixed_time_kinds_rejected(self):
        with redirect_stderr(io.StringIO()):
            self.assertRaises(SystemExit, args, "--created-before", "2026-01-01", "--updated-before", "2026-01-01")

    def test_merges_sources_newest_first(self):
        times = [m.updated(c) for c in m.find_chats(FakeApi(), args())[0]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_limit_counts_only_chats_to_delete(self):
        to_delete, out, stopped = m.find_chats(FakeApi(), args("--exclude-pinned", "--limit", "3"))
        self.assertEqual((len(to_delete), [c["id"] for c, _ in out], stopped), (3, ["a2"], True))

    def test_not_stopped_when_all_read(self):
        self.assertFalse(m.find_chats(FakeApi(), args("--limit", "99"))[2])
        self.assertFalse(m.find_chats(FakeApi(), args("--limit", "5"))[2])

    def dry_run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "Api", return_value=FakeApi()), \
                mock.patch.object(m, "out", Console(file=out, width=200)), \
                mock.patch.object(m, "err", Console(file=err, width=200)):
            self.assertEqual(m.main(["--authorization", "h.p.s", "--dry-run", *argv]), 0)
        return out.getvalue(), err.getvalue()

    def test_dry_run_output(self):
        out, err = self.dry_run("--exclude-pinned", "--exclude-title-contains", "only")
        self.assertIn("Reason", err)
        self.assertRegex(err, r"a2 .* pinned .* Pinned chat")
        self.assertRegex(err, r"p2 .* title has 'only' .* Project only")
        self.assertIn("3 to delete · 2 excluded", err)
        self.assertIn("Read 5 chats", err)
        self.assertIn("To delete (3)", out)
        self.assertNotIn("Reason", out)
        self.assertNotIn("a2", out)

    def test_table_keeps_ids_and_raw_titles_at_80_columns(self):
        buf = io.StringIO()
        c = chat("6720a1b2-0000-8000-aaaa-123456789abc", "Fix :x: bug " + "long " * 20)
        Console(file=buf, width=80, emoji=False).print(m.table([c], "t", width=80))
        self.assertIn("6720a1b2-0000-8000-aaaa-123456789abc", buf.getvalue())
        self.assertIn("Fix :x: bug", buf.getvalue())

    def test_table_hides_empty_columns_and_counts_reasons(self):
        buf = io.StringIO()
        chats = [chat("a", "Line one\nline two"), chat("b", "B")]
        Console(file=buf, width=200).print(m.table(chats, "t", ["pinned", "pinned"]))
        text = buf.getvalue()
        self.assertNotIn("Project", text)
        self.assertNotIn("Pinned", text)
        self.assertNotIn("Archived", text)
        self.assertIn("pinned: 2", text)
        Console(file=io.StringIO()).print(m.table(chats, "t", ["title has '[/]'", "pinned"]))  # No MarkupError.
        self.assertIn("Line one line two", text)

    def test_title_and_reason_visible_at_80_columns(self):
        buf = io.StringIO()
        c = chat("6720a1b2-0000-8000-aaaa-123456789abc", "Перевод рецепта печенья на немецкий язык",
                 pinned_time="2026-01-01T00:00:00Z", project="Work")
        Console(file=buf, width=80, emoji=False).print(m.table([c], "t", ["title has 'рецепт'"], width=80))
        self.assertIn("Перевод", buf.getvalue())
        self.assertIn("6720a1b2-0000-8000-aaaa-123456789abc", buf.getvalue())
        self.assertTrue(all(len(line) <= 80 for line in buf.getvalue().splitlines()))

    def test_backslash_title_keeps_link(self):
        buf = io.StringIO()
        Console(file=buf, width=200).print(m.table([chat("x1", "trail\\\\\\")], "t", width=200))
        self.assertIn("trail\\\\\\", buf.getvalue())
        self.assertNotIn("[/link]", buf.getvalue())

    def test_ids_and_titles_link_to_chat(self):
        buf = io.StringIO()
        c = chat("6720a1b2-0000-8000-aaaa-123456789abc", "Hello", gizmo_id="g-p-1", project="Work")
        Console(file=buf, width=200, force_terminal=True).print(m.table([c], "t", width=200))
        self.assertEqual(buf.getvalue().count("https://chatgpt.com/c/6720a1b2-0000-8000-aaaa-123456789abc"), 2)
        self.assertIn("https://chatgpt.com/g/g-p-1/project", buf.getvalue())

    def test_dry_run_reports_limit_stop(self):
        _, err = self.dry_run("--exclude-pinned", "--limit", "1")
        self.assertIn("Stopped at --limit: older chats not checked", err)


class Resp(io.BytesIO):
    status = 200


class Errors(unittest.TestCase):
    def test_retries_timeouts(self):
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError(), Resp(b'{"ok": 1}')]), \
                self.assertLogs(m.log, "DEBUG"):
            self.assertEqual(m.Api("x", delay=0, retry_delay=0).call("GET", "me"), {"ok": 1})

    def test_logs_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError(), Resp(b"{}")]), \
                self.assertLogs(m.log, "DEBUG") as logs:
            m.Api("x", delay=0, retry_delay=0).call("GET", "me")
        self.assertTrue(any("retry 1/4 in 0.0s" in line for line in logs.output))
        self.assertTrue(any("GET me -> 200" in line for line in logs.output))

    def test_countdown_shows_time_left(self):
        shown = []
        with mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0, 0, 1, 2]):
            m.countdown(shown.append, "Rate limited", 2)
        self.assertEqual(shown, ["[yellow]Rate limited in 2s…[/]", "[yellow]Rate limited in 1s…[/]"])

    def test_token_expiry(self):
        payload = base64.urlsafe_b64encode(b'{"exp": 0}').decode().rstrip("=")
        self.assertEqual(m.token_expiry(f"h.{payload}.s"), datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(m.token_expiry("not-a-jwt"))

    def test_main_exits_2_on_api_error(self):
        broken = mock.Mock(call=mock.Mock(side_effect=RuntimeError("HTTP Error 403")))
        err = io.StringIO()
        with mock.patch.object(m, "Api", return_value=broken), redirect_stderr(err):
            self.assertEqual(m.main(["--authorization", "h.p.s", "--dry-run"]), 2)
        self.assertIn("error: HTTP Error 403", err.getvalue())


class Pick(unittest.TestCase):
    def test_enter_keeps_and_writes_n_after_wrapped_prompt(self):
        out = io.StringIO()
        chats = [chat("a1", "party :smile: time"), chat("a2", "x")]
        with mock.patch.object(m, "err", Console(file=out, width=40, force_terminal=True)), \
                mock.patch.object(m, "ask", side_effect=["", "y"]):
            self.assertEqual([c["id"] for c in m.pick(chats)], ["a2"])
        # Prompt wraps after 'time'?, last line '[y/N/a/q] ' is 10 cells: N goes to column 11.
        self.assertEqual(out.getvalue(), "\x1b[1A\x1b[11GN\x1b[1E")


class Delete(unittest.TestCase):
    chats = [chat(i, i) for i in ("c1", "c2", "c3")]

    def run_delete(self, **kw):
        with redirect_stderr(io.StringIO()):
            d, f = m.delete_all(FakeApi(fail_ids={"c2"}), self.chats, **kw)
        return [c["id"] for c in d], [c["id"] for c in f]

    def test_stops_on_first_error(self):
        self.assertEqual(self.run_delete(), (["c1"], ["c2"]))

    def test_continue_on_error(self):
        self.assertEqual(self.run_delete(continue_on_error=True), (["c1", "c3"], ["c2"]))

    def test_summary_after_auth_error(self):
        class Expired(FakeApi):
            def call(self, method, path, body=None):
                raise m.AuthError("HTTP 401")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(m.AuthError):
            m.delete_all(Expired(), self.chats)
        self.assertIn("deleted 0 · ✖ failed 0 · skipped 3", err.getvalue())

    def test_sends_soft_delete_patch(self):
        api = FakeApi()
        with redirect_stderr(io.StringIO()):
            m.delete_all(api, self.chats[:1])
        self.assertEqual(api.calls, [("PATCH", "conversation/c1")])


if __name__ == "__main__":
    unittest.main()
