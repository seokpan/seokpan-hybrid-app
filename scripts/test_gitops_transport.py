#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/gitops_transport.py (fake HTTP + real git against a local bare repository)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import gitops_transport as target  # noqa: E402
import gitops_writer as writer  # noqa: E402
import test_gitops_writer as wx  # noqa: E402

REPO = "seokpan/seokpan-hybrid-gitops"
USER = "ci-user"
TOKEN = "github_pat_" + "Zy9X8w7V6u" * 5
SHA_A = "a" * 40
BRANCH = "promotion/rel-recovery-20261008T030000Z-46e21a74dd60-a1b2c3d4"
HAVE_GIT = shutil.which("git") is not None


class FakeHttp:
    """Scripted HTTP function. Responses: list of (status, body) consumed in order, or a callable."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list = []

    def __call__(self, method, url, headers, data):
        self.requests.append((method, url, dict(headers), data))
        item = self.responses.pop(0) if self.responses else (500, b"{}")
        if callable(item):
            return item(method, url, headers, data)
        status, body = item
        return status, (body if isinstance(body, bytes) else json.dumps(body).encode())


def make(http, checkout: Path = Path("/nonexistent")) -> target.GitHubApiTransport:
    return target.GitHubApiTransport(REPO, USER, TOKEN, checkout, http=http)


def pull_item(number=1, head=BRANCH, state="open", merged_at=None) -> dict:
    return {
        "number": number, "state": state, "merged_at": merged_at,
        "head": {"ref": head}, "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


class ConstructionTests(unittest.TestCase):
    def test_repository_allowlist_and_credentials(self) -> None:
        with self.assertRaises(writer.TransportError):
            target.GitHubApiTransport("seokpan/other", USER, TOKEN, Path("."))
        with self.assertRaises(writer.TransportError):
            target.GitHubApiTransport(REPO, "", TOKEN, Path("."))
        with self.assertRaises(writer.TransportError):
            target.GitHubApiTransport(REPO, USER, "", Path("."))

    def test_repr_never_contains_the_credential(self) -> None:
        self.assertNotIn(TOKEN, repr(make(FakeHttp())))
        self.assertNotIn(USER, repr(make(FakeHttp())))

    def test_from_env_reads_the_named_variables_only(self) -> None:
        tr = target.GitHubApiTransport.from_env({"GH_USER": USER, "GH_TOKEN": TOKEN}, Path("."), http=FakeHttp())
        self.assertIsInstance(tr, target.GitHubApiTransport)
        with self.assertRaises(writer.TransportError):
            target.GitHubApiTransport.from_env({}, Path("."))


class RestTests(unittest.TestCase):
    def test_get_branch_sha(self) -> None:
        http = FakeHttp((200, {"object": {"sha": SHA_A}}), (404, {"message": "Not Found"}))
        tr = make(http)
        self.assertEqual(tr.get_branch_sha("main"), SHA_A)
        self.assertIsNone(tr.get_branch_sha(BRANCH))
        method, url, headers, data = http.requests[0]
        self.assertEqual((method, data), ("GET", None))
        self.assertEqual(url, f"https://api.github.com/repos/{REPO}/git/ref/heads/main")
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertIn(f"/git/ref/heads/{BRANCH}", http.requests[1][1])

    def test_get_branch_sha_rejects_odd_responses_and_names(self) -> None:
        for response in ((200, []), (200, {"object": {"sha": "short"}}), (500, {"message": "boom"})):
            with self.assertRaises(writer.TransportError):
                make(FakeHttp(response)).get_branch_sha("main")
        for bad in ("../x", "a b", "/abs", "x/", "x..y", ""):
            http = FakeHttp()
            with self.assertRaises(writer.TransportError):
                make(http).get_branch_sha(bad)
            self.assertEqual(http.requests, [])

    def test_find_pull_requests_maps_state_and_merge(self) -> None:
        http = FakeHttp((200, [pull_item(3, state="closed", merged_at="2026-10-08T00:00:00Z"), pull_item(4, state="closed"), pull_item(5, head="other")]))
        found = make(http).find_pull_requests(BRANCH)
        self.assertEqual([(p.number, p.state, p.merged) for p in found], [(3, "closed", True), (4, "closed", False)])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(http.requests[0][1]).query)
        self.assertEqual(query["state"], ["all"])
        self.assertEqual(query["head"], [f"seokpan:{BRANCH}"])

    def test_find_pull_requests_fails_closed_on_truncated_or_bad_data(self) -> None:
        for response in ((200, [pull_item(i) for i in range(100)]), (200, {"x": 1}), (200, [{"number": 1}]), (403, {"message": "denied"})):
            with self.assertRaises(writer.TransportError):
                make(FakeHttp(response)).find_pull_requests(BRANCH)

    def test_list_open_pull_requests_paginates(self) -> None:
        page1 = (200, [pull_item(i, head=f"feature/{i}") for i in range(100)])
        page2 = (200, [pull_item(500, head="feature/last")])
        http = FakeHttp(page1, page2)
        found = make(http).list_open_pull_requests()
        self.assertEqual(len(found), 101)
        pages = [urllib.parse.parse_qs(urllib.parse.urlparse(r[1]).query)["page"] for r in http.requests]
        self.assertEqual(pages, [["1"], ["2"]])

    def test_list_open_pull_requests_stops_at_the_page_limit(self) -> None:
        full = [(200, [pull_item(i, head=f"feature/{i}") for i in range(100)])] * target.MAX_PAGES
        with self.assertRaises(writer.TransportError):
            make(FakeHttp(*full)).list_open_pull_requests()

    def test_file_exists(self) -> None:
        tr = make(FakeHttp((200, {}), (404, {"message": "Not Found"}), (500, {})))
        self.assertTrue(tr.file_exists("releases/x.json", SHA_A))
        self.assertFalse(tr.file_exists("releases/x.json", SHA_A))
        with self.assertRaises(writer.TransportError):
            tr.file_exists("releases/x.json", SHA_A)

    def test_file_exists_rejects_unsafe_paths_without_a_request(self) -> None:
        for bad in ("../x", "/abs", "a/../b", ".git/config", "a\\b", ""):
            http = FakeHttp()
            with self.assertRaises(writer.TransportError):
                make(http).file_exists(bad, SHA_A)
            self.assertEqual(http.requests, [])

    def test_create_pull_request(self) -> None:
        http = FakeHttp((201, {"html_url": "https://github.com/seokpan/seokpan-hybrid-gitops/pull/9"}))
        url = make(http).create_pull_request(BRANCH, "main", "title", "body")
        self.assertTrue(url.endswith("/pull/9"))
        method, _, _, data = http.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(json.loads(data), {"title": "title", "head": BRANCH, "base": "main", "body": "body"})

    def test_pull_request_guards(self) -> None:
        for head, base in (("feature/x", "main"), (BRANCH, "release")):
            http = FakeHttp()
            with self.assertRaises(writer.TransportError):
                make(http).create_pull_request(head, base, "t", "b")
            self.assertEqual(http.requests, [])
        for response in ((422, {"message": "Validation Failed"}), (201, {})):
            with self.assertRaises(writer.TransportError):
                make(FakeHttp(response)).create_pull_request(BRANCH, "main", "t", "b")

    def test_delete_branch_only_promotion_branches(self) -> None:
        http = FakeHttp((204, b""))
        make(http).delete_branch(BRANCH)
        self.assertEqual(http.requests[0][0], "DELETE")
        for bad in ("main", "feature/x"):
            guarded = FakeHttp()
            with self.assertRaises(writer.TransportError):
                make(guarded).delete_branch(bad)
            self.assertEqual(guarded.requests, [])
        with self.assertRaises(writer.TransportError):
            make(FakeHttp((403, {"message": "denied"}))).delete_branch(BRANCH)


class SecurityTests(unittest.TestCase):
    def test_every_request_goes_to_api_github_com_with_the_token_only_in_the_header(self) -> None:
        http = FakeHttp((200, {"object": {"sha": SHA_A}}), (200, []), (200, {}), (201, {"html_url": "https://x/y"}), (204, b""))
        tr = make(http)
        tr.get_branch_sha("main")
        tr.find_pull_requests(BRANCH)
        tr.file_exists("a/b.yaml", SHA_A)
        tr.create_pull_request(BRANCH, "main", "t", "b")
        tr.delete_branch(BRANCH)
        for method, url, headers, data in http.requests:
            self.assertTrue(url.startswith("https://api.github.com/repos/seokpan/seokpan-hybrid-gitops"))
            self.assertNotIn(TOKEN, url)
            self.assertNotIn(TOKEN, (data or b"").decode())
            self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")

    def test_server_echoing_the_token_never_reaches_an_error_message(self) -> None:
        echoed = (401, {"message": f"Bad credentials for Bearer {TOKEN}"})
        with self.assertRaises(writer.TransportError) as ctx:
            make(FakeHttp(echoed)).get_branch_sha("main")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_redirects_are_not_followed(self) -> None:
        handler = target._NoRedirect()
        self.assertIsNone(handler.redirect_request(mock.Mock(), None, 301, "Moved", {}, "https://evil.example/"))
        with self.assertRaises(writer.TransportError):
            make(FakeHttp((301, {"message": "Moved Permanently"}))).get_branch_sha("main")

    def test_non_json_success_body_is_an_error(self) -> None:
        with self.assertRaises(writer.TransportError):
            make(FakeHttp((200, b"<html>"))).get_branch_sha("main")

    def test_askpass_contains_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = target.make_askpass(Path(d))
            text = path.read_text(encoding="utf-8")
            self.assertIn(target.TOKEN_ENV, text)
            self.assertNotIn(TOKEN, text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_source_does_not_use_requests_or_force(self) -> None:
        src = (Path(__file__).resolve().parent / "gitops_transport.py").read_text(encoding="utf-8")
        self.assertNotIn("import requests", src)
        self.assertNotIn("--force", src)
        self.assertNotIn("+HEAD", src)


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=check, capture_output=True, text=True,
    ).stdout.strip()


class GitFixture:
    """Real git: a local bare 'origin' and a clean checkout of its main branch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bare = root / "origin.git"
        seed = root / "seed"
        git("init", "--bare", "-b", "main", str(self.bare), cwd=root)
        git("clone", str(self.bare), str(seed), cwd=root)
        rec = seed / gitops_planner.ENV_KUSTOMIZATION["recovery"]
        rec.parent.mkdir(parents=True)
        with open(rec, "w", encoding="utf-8", newline="") as f:
            f.write(wx.fx.RECOVERY_KUST)
        git("add", "-A", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:refs/heads/main", cwd=seed)
        self.checkout = root / "work"
        git("clone", str(self.bare), str(self.checkout), cwd=root)
        self.base = git("rev-parse", "HEAD", cwd=self.checkout)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def transport(self, http=None) -> target.GitHubApiTransport:
        return make(http or FakeHttp(), self.checkout)

    def bare_has(self, branch: str) -> bool:
        return subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=self.bare, capture_output=True
        ).returncode == 0

    def show(self, branch: str, path: str) -> bytes:
        return subprocess.run(["git", "show", f"{branch}:{path}"], cwd=self.bare, capture_output=True, check=True).stdout

@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitTests(GitFixture, unittest.TestCase):
    def test_push_commit_creates_the_branch_with_exact_bytes_and_leaves_main(self) -> None:
        path = gitops_planner.ENV_KUSTOMIZATION["recovery"]
        new_text = wx.fx.RECOVERY_KUST.replace(wx.fx.OLD_BE, wx.NEW_BE).replace("\n", "\r\n")
        files = {path: new_text, "releases/rel-x.json": '{"a": 1}\n'}
        sha = self.transport().push_commit(BRANCH, self.base, files, "chore(promotion): test")
        self.assertTrue(self.bare_has(BRANCH))
        self.assertEqual(git("rev-parse", BRANCH, cwd=self.bare), sha)
        self.assertEqual(self.show(BRANCH, path), new_text.encode("utf-8"))  # CRLF preserved
        self.assertEqual(self.show(BRANCH, "releases/rel-x.json"), b'{"a": 1}\n')
        self.assertEqual(git("rev-parse", "main", cwd=self.bare), self.base)  # main untouched
        self.assertEqual(git("log", "-1", "--format=%an <%ae>|%s", BRANCH, cwd=self.bare),
                         "seokpan-jenkins <seokpan-jenkins@users.noreply.github.com>|chore(promotion): test")
        self.assertEqual(git("rev-parse", f"{BRANCH}^", cwd=self.bare), self.base)

    def test_push_uses_askpass_env_and_never_force(self) -> None:
        calls: list = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            calls.append((list(cmd), dict(kw.get("env") or {})))
            return real_run(cmd, *a, **kw)

        recorded: list = []
        real_tmp = tempfile.TemporaryDirectory

        class Recording(real_tmp):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                recorded.append(self.name)

        with mock.patch.object(target.subprocess, "run", spy), mock.patch.object(target.tempfile, "TemporaryDirectory", Recording):
            self.transport().push_commit(BRANCH, self.base, {"releases/rel-x.json": "{}\n"}, "m")
        push = [c for c in calls if "push" in c[0]]
        self.assertEqual(len(push), 1)
        cmd, env = push[0]
        self.assertEqual(cmd[-2:], ["origin", f"HEAD:refs/heads/{BRANCH}"])
        self.assertNotIn("--force", cmd)
        self.assertEqual(env[target.TOKEN_ENV], TOKEN)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(env["GIT_ASKPASS"].endswith("git-askpass.sh"))
        others = [c for c in calls if "push" not in c[0]]
        self.assertTrue(all(target.TOKEN_ENV not in c[1] for c in others))
        self.assertEqual(len(recorded), 1)
        self.assertFalse(Path(recorded[0]).exists())  # askpass directory removed

    def test_refuses_when_head_is_not_the_base(self) -> None:
        with self.assertRaises(writer.TransportError):
            self.transport().push_commit(BRANCH, "b" * 40, {"releases/rel-x.json": "{}\n"}, "m")
        self.assertFalse(self.bare_has(BRANCH))

    def test_refuses_a_dirty_checkout(self) -> None:
        (self.checkout / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(writer.TransportError):
            self.transport().push_commit(BRANCH, self.base, {"releases/rel-x.json": "{}\n"}, "m")
        self.assertFalse(self.bare_has(BRANCH))

    def test_refuses_when_a_requested_file_does_not_change(self) -> None:
        path = gitops_planner.ENV_KUSTOMIZATION["recovery"]
        with self.assertRaises(writer.TransportError):
            self.transport().push_commit(BRANCH, self.base, {path: wx.fx.RECOVERY_KUST, "releases/rel-x.json": "{}\n"}, "m")
        self.assertFalse(self.bare_has(BRANCH))

    def test_existing_remote_branch_is_rejected_not_overwritten(self) -> None:
        self.transport().push_commit(BRANCH, self.base, {"releases/rel-x.json": "{}\n"}, "first")
        first = git("rev-parse", BRANCH, cwd=self.bare)
        git("switch", "main", cwd=self.checkout)
        git("branch", "-D", BRANCH, cwd=self.checkout)
        with self.assertRaises(writer.TransportError) as ctx:
            self.transport().push_commit(BRANCH, self.base, {"releases/rel-y.json": "{}\n"}, "second")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertEqual(git("rev-parse", BRANCH, cwd=self.bare), first)

    def test_guards_on_branch_path_and_size(self) -> None:
        tr = self.transport()
        for branch, files in (
            ("main", {"releases/a.json": "{}"}),
            (BRANCH, {"../escape": "x"}),
            (BRANCH, {".git/config": "x"}),
            (BRANCH, {}),
            (BRANCH, {f"releases/{i}.json": "{}" for i in range(target.MAX_FILES + 1)}),
            (BRANCH, {"releases/a.json": 1}),
        ):
            with self.assertRaises(writer.TransportError):
                tr.push_commit(branch, self.base, files, "m")
        with self.assertRaises(writer.TransportError):
            tr.push_commit(BRANCH, "short", {"releases/a.json": "{}"}, "m")
        with self.assertRaises(writer.TransportError):
            tr.push_commit(BRANCH, self.base, {"releases/a.json": "{}"}, "  ")
        self.assertFalse(self.bare_has(BRANCH))


class FakeGitHub:
    """Minimal GitHub REST emulation on top of a local bare repository."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: list = []
        self.requests: list = []

    def _git_ok(self, *args) -> tuple:
        proc = subprocess.run(["git", *args], cwd=self.bare, capture_output=True, text=True)
        return proc.returncode == 0, proc.stdout.strip()

    def __call__(self, method, url, headers, data):
        self.requests.append((method, url, dict(headers)))
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)[len(f"/repos/{REPO}"):]
        query = urllib.parse.parse_qs(parsed.query)
        if method == "GET" and path.startswith("/git/ref/heads/"):
            ok, sha = self._git_ok("rev-parse", "--verify", "refs/heads/" + path[len("/git/ref/heads/"):])
            return (200, json.dumps({"object": {"sha": sha}}).encode()) if ok else (404, b'{"message": "Not Found"}')
        if method == "GET" and path.startswith("/contents/"):
            ok, _ = self._git_ok("cat-file", "-e", f"{query['ref'][0]}:{path[len('/contents/'):]}")
            return (200, b"{}") if ok else (404, b'{"message": "Not Found"}')
        if method == "GET" and path == "/pulls":
            state = query["state"][0]
            head = query.get("head", [""])[0].split(":", 1)[-1]
            rows = [p for p in self.prs if state in ("all", p["state"]) and (not head or p["head"]["ref"] == head)]
            return 200, json.dumps(rows).encode()
        if method == "POST" and path == "/pulls":
            body = json.loads(data)
            number = len(self.prs) + 1
            self.prs.append({
                "number": number, "state": "open", "merged_at": None, "head": {"ref": body["head"]},
                "html_url": f"https://github.com/{REPO}/pull/{number}", "_body": body,
            })
            return 201, json.dumps(self.prs[-1]).encode()
        return 500, b'{"message": "unexpected request"}'


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class EndToEndTests(GitFixture, unittest.TestCase):
    """planner -> release candidate -> writer -> real transport (fake GitHub REST, real git)."""

    def setUp(self) -> None:
        super().setUp()
        self.gh = FakeGitHub(self.bare)
        self.tr = make(self.gh, self.checkout)
        files = {gitops_planner.ENV_KUSTOMIZATION["recovery"]: (self.checkout / gitops_planner.ENV_KUSTOMIZATION["recovery"]).read_bytes().decode("utf-8")}
        self.plan = gitops_planner.plan_promotion(wx.meta(), "recovery", ["backend", "frontend"], files)

    def run_writer(self, write: bool = True):
        return writer.write_promotion(
            self.tr, self.plan, wx.meta(), wx.RID, wx.make_candidate(), self.base, write=write
        )

    def test_dry_run_changes_nothing_remote(self) -> None:
        result = self.run_writer(write=False)
        self.assertEqual(result.status, "DRY_RUN")
        self.assertFalse(self.bare_has(wx.BRANCH))
        self.assertEqual(self.gh.prs, [])

    def test_full_flow_pushes_files_and_opens_one_pr(self) -> None:
        result = self.run_writer()
        self.assertEqual(result.status, "PR_CREATED")
        self.assertEqual(result.pr_url, f"https://github.com/{REPO}/pull/1")
        self.assertTrue(self.bare_has(wx.BRANCH))
        path = gitops_planner.ENV_KUSTOMIZATION["recovery"]
        self.assertEqual(self.show(wx.BRANCH, path).decode("utf-8"), self.plan.new_files[path])
        self.assertIn(wx.NEW_BE, self.show(wx.BRANCH, path).decode("utf-8"))
        self.assertEqual(self.show(wx.BRANCH, wx.REL_PATH).decode("utf-8"), wx.make_candidate())
        self.assertEqual(git("rev-parse", "main", cwd=self.bare), self.base)
        self.assertEqual(self.gh.prs[0]["_body"]["head"], wx.BRANCH)
        self.assertEqual(self.gh.prs[0]["_body"]["base"], "main")
        self.assertNotIn(TOKEN, json.dumps(self.gh.prs))
        self.assertTrue(all(h["Authorization"] == f"Bearer {TOKEN}" for _, _, h in self.gh.requests))
        self.assertEqual({urllib.parse.urlparse(u).netloc for _, u, _ in self.gh.requests}, {"api.github.com"})

    def test_second_run_is_blocked_by_the_open_pr_without_new_pushes(self) -> None:
        self.run_writer()
        before = git("rev-parse", wx.BRANCH, cwd=self.bare)
        with self.assertRaises(writer.WriterError) as ctx:
            self.run_writer()
        self.assertEqual(ctx.exception.code, "PROMOTION_OPEN_PR_REQUIRES_REVIEW")
        self.assertEqual(git("rev-parse", wx.BRANCH, cwd=self.bare), before)
        self.assertEqual(len(self.gh.prs), 1)

    def test_base_moved_is_detected_before_any_push(self) -> None:
        with self.assertRaises(writer.WriterError) as ctx:
            writer.write_promotion(self.tr, self.plan, wx.meta(), wx.RID, wx.make_candidate(), "e" * 40, write=True)
        self.assertEqual(ctx.exception.code, "PROMOTION_BASE_MOVED")
        self.assertFalse(self.bare_has(wx.BRANCH))

    def test_pr_failure_deletes_only_the_new_branch(self) -> None:
        original = self.gh.__call__

        def failing(method, url, headers, data):
            if method == "POST":
                return 422, b'{"message": "Validation Failed"}'
            if method == "DELETE":
                ref = urllib.parse.unquote(urllib.parse.urlparse(url).path).split("/git/refs/heads/", 1)[1]
                subprocess.run(["git", "update-ref", "-d", f"refs/heads/{ref}"], cwd=self.bare, check=True)
                return 204, b""
            return original(method, url, headers, data)

        self.tr = make(failing, self.checkout)
        with self.assertRaises(writer.WriterError) as ctx:
            self.run_writer()
        self.assertEqual(ctx.exception.code, "PROMOTION_PR_CREATE_FAILED")
        self.assertIn("branch deleted", str(ctx.exception))
        self.assertFalse(self.bare_has(wx.BRANCH))
        self.assertTrue(self.bare_has("main"))


if __name__ == "__main__":
    unittest.main()
