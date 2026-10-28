"""Tests with a fake API. No network. Run: python3 -m unittest -v"""
import base64
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import chatgpt_chats_bulk_remove as m


def chat(id, title, created="2026-01-10T10:00:00.000000Z", updated=None, **kw):
    return {"id": id, "title": title, "create_time": created, "update_time": updated or created,
            "pinned_time": None, "is_archived": False, "gizmo_id": None, **kw}


ACTIVE = [chat("a1", "Plain chat"), chat("a2", "Pinned chat", pinned_time="2026-02-01T00:00:00Z"),
          chat("p1", "Project chat in main list", gizmo_id="g-p-1")]
ARCHIVED = [chat("r1", "Old archived", created="2025-05-01T00:00:00Z", is_archived=True)]
PROJECT = [chat("p1", "Project chat in main list", gizmo_id="g-p-1"), chat("p2", "Project only", gizmo_id="g-p-1")]


class FakeApi:
    def __init__(self, fail_ids=()):
        self.calls, self.fail_ids = [], set(fail_ids)

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "PATCH":
            if path.split("/")[1] in self.fail_ids:
                raise RuntimeError("boom")
            return {"success": True}
        if path.startswith("conversations?"):
            return {"items": ARCHIVED if "is_archived=true" in path else ACTIVE}
        if path.startswith("gizmos/snorlax/sidebar"):
            return {"items": [{"gizmo": {"gizmo": {"id": "g-p-1", "display": {"name": "Work"}}}}], "cursor": None}
        if path.startswith("gizmos/g-p-1/conversations"):
            return {"items": PROJECT, "cursor": None}
        raise AssertionError(path)


def args(*argv):
    return m.parse_args(["--authorization", "x", *argv])


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
        self.assertEqual(m.parse_date("2026-10-29", end=True), datetime(2026, 10, 29, 23, 59, 59, 999999, tzinfo=utc))
        self.assertEqual(m.parse_date("2026-10-29T09:03:50.719809Z"), datetime(2026, 10, 29, 9, 3, 50, 719809, tzinfo=utc))
        self.assertEqual(m.parse_date("20261029", end=True), datetime(2026, 10, 29, 23, 59, 59, 999999, tzinfo=utc))
        self.assertEqual(m.parse_date("2026-10-29T00:00:00", end=True), datetime(2026, 10, 29, tzinfo=utc))

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
        with mock.patch.dict(os.environ, {"CHATGPT_ACCESS_TOKEN": "env"}):
            self.assertEqual(m.parse_args(["--authorization", "flag"]).authorization, "flag")
            self.assertEqual(m.parse_args([]).authorization, "env")
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
            self.assertRaises(SystemExit, m.parse_args, [])
            self.assertRaises(SystemExit, m.parse_args, ["--authorization", "Bearer x"])
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
        self.assertEqual(select("--create-time-to", "2025-05-01"), ["r1"])
        self.assertEqual(select("--create-time-from", "2026-01-10T10:00:00.000001Z"), [])

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
        self.assertEqual(excluded("--project-name", "Work", "--update-time-to", "2025-12-31"), {})

    def test_title_contains_selects_silently(self):
        self.assertEqual(excluded("--title-contains", "plain"), {})


class PagedApi(FakeApi):
    """Main list: 2 full pages, chat c<i> updated i days before 2026-10-01. No archive, no projects."""

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if "is_archived=false" in path:
            start = int(path.split("offset=")[1].split("&")[0])
            day0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
            return {"items": [chat(f"c{i}", f"c{i}", updated=(day0 - timedelta(days=i)).isoformat())
                              for i in range(start, min(start + m.PAGE, 2 * m.PAGE))]}
        return {"items": [], "cursor": None}


class EarlyStop(unittest.TestCase):
    def test_limit_reads_only_needed_pages(self):
        api = PagedApi()
        self.assertEqual([c["id"] for c in m.find_chats(api, args("--limit", "3"))[0]], ["c0", "c1", "c2"])
        self.assertFalse(any("offset=28" in p for _, p in api.calls))

    def test_update_time_from_stops_paging(self):
        api = PagedApi()
        self.assertEqual(len(m.find_chats(api, args("--update-time-from", "2026-09-10"))[0]), 22)
        self.assertFalse(any("offset=28" in p for _, p in api.calls))

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
        with mock.patch.object(m, "Api", return_value=FakeApi()), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(m.main(["--authorization", "x", "--dry-run", *argv]), 0)
        return out.getvalue(), err.getvalue()

    def test_dry_run_output(self):
        out, err = self.dry_run("--exclude-pinned", "--exclude-title-contains", "only")
        self.assertIn("REASON", err)
        self.assertRegex(err, r"a2 .* pinned +Pinned chat")
        self.assertRegex(err, r"p2 .* title has 'only' +Project only")
        self.assertIn("3 to delete, 2 excluded\n", err)
        self.assertNotIn("REASON", out)
        self.assertNotIn("a2", out)

    def test_dry_run_reports_limit_stop(self):
        _, err = self.dry_run("--exclude-pinned", "--limit", "1")
        self.assertIn("Stopped at --limit: older chats not checked", err)


class Resp(io.BytesIO):
    status = 200


class Errors(unittest.TestCase):
    def test_retries_timeouts(self):
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError(), Resp(b'{"ok": 1}')]):
            self.assertEqual(m.Api("x", delay=0, retry_delay=0).call("GET", "me"), {"ok": 1})

    def test_logs_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError(), Resp(b"{}")]), \
                self.assertLogs(m.log, "DEBUG") as logs:
            m.Api("x", delay=0, retry_delay=0).call("GET", "me")
        self.assertTrue(any("retry 1/3 in 0.0s" in line for line in logs.output))
        self.assertTrue(any("GET me -> 200" in line for line in logs.output))

    def test_token_expiry(self):
        payload = base64.urlsafe_b64encode(b'{"exp": 0}').decode().rstrip("=")
        self.assertEqual(m.token_expiry(f"h.{payload}.s"), datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(m.token_expiry("not-a-jwt"))

    def test_main_exits_2_on_api_error(self):
        broken = mock.Mock(call=mock.Mock(side_effect=RuntimeError("HTTP Error 403")))
        err = io.StringIO()
        with mock.patch.object(m, "Api", return_value=broken), redirect_stderr(err):
            self.assertEqual(m.main(["--authorization", "x", "--dry-run"]), 2)
        self.assertIn("error: HTTP Error 403", err.getvalue())


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
        self.assertIn("deleted 0, failed 0, skipped 3", err.getvalue())

    def test_sends_soft_delete_patch(self):
        api = FakeApi()
        with redirect_stderr(io.StringIO()):
            m.delete_all(api, self.chats[:1])
        self.assertEqual(api.calls, [("PATCH", "conversation/c1")])


if __name__ == "__main__":
    unittest.main()
