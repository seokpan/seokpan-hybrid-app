#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""hybrid-gitops GitHub Transport for the Promotion Writer (hybrid-app #15).

`gitops_writer.GitHubTransport` 의 실제 구현이다.
- 읽기/PR/Branch 삭제: GitHub REST (urllib, Bearer, 30초 timeout, redirect 따라가지 않음)
- Branch 생성/Commit/Push: 이미 Planner 가 읽은 로컬 checkout 에서 git CLI + GIT_ASKPASS
  (D2 Build 3 에서 Push/PR 동작이 검증된 경로)

안전 규칙: 대상 저장소는 allowlist 한 곳, 요청은 api.github.com 으로만 보내고 redirect 를 따라가지
않는다. Branch 는 `promotion/` 만 생성/삭제하고 PR base 는 main 뿐이다. force push 와 main
Push 는 없다. 토큰은 Authorization 헤더와 git 하위 프로세스 환경 변수로만 쓰이며 오류 문자열은
마스킹한다. 이 모듈은 Jenkinsfile 에 연결되지 않았다(후속).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_writer as writer  # noqa: E402

API_BASE = "https://api.github.com"
ALLOWED_REPOS = ("seokpan/seokpan-hybrid-gitops",)
USER_ENV = "GITOPS_GITHUB_USER"
TOKEN_ENV = "GITOPS_GITHUB_TOKEN"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_FILES = 20
MAX_PAGES = 10
PER_PAGE = 100

HttpFn = Callable[[str, str, Mapping[str, str], Optional[bytes]], tuple]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Authorization header must not leave api.github.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def urllib_http(method: str, url: str, headers: Mapping[str, str], data: Optional[bytes]) -> tuple:
    """Default HTTP function: returns (status, body). Network failures raise TransportError."""
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # URLError, timeout, TLS
        raise writer.TransportError(f"network error: {type(exc).__name__}")


def make_askpass(directory: Path) -> Path:
    """The helper only echoes environment variables; it never contains the secret itself."""
    path = directory / "git-askpass.sh"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  *Username*) printf "%s\\n" "${USER_ENV}" ;;\n'
        f'  *Password*) printf "%s\\n" "${TOKEN_ENV}" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _check_branch(branch: str, *, promotion_only: bool) -> None:
    if (
        not isinstance(branch, str)
        or not BRANCH_RE.match(branch)
        or ".." in branch
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".lock")
    ):
        raise writer.TransportError("invalid branch name")
    if promotion_only and not branch.startswith(writer.BRANCH_PREFIX):
        raise writer.TransportError(f"only {writer.BRANCH_PREFIX}* branches may be created or deleted")


def _check_path(path: str) -> str:
    pure = PurePosixPath(path) if isinstance(path, str) else None
    if (
        pure is None
        or not path
        or pure.is_absolute()
        or any(part in ("", ".", "..", ".git") for part in pure.parts)
        or "\\" in path
        or "\x00" in path
    ):
        raise writer.TransportError("invalid file path")
    return str(pure)


class GitHubApiTransport(writer.GitHubTransport):
    def __init__(
        self,
        repo: str,
        user: str,
        token: str,
        checkout: Path,
        http: Optional[HttpFn] = None,
        git_bin: str = "git",
        author_name: str = "seokpan-jenkins",
        author_email: str = "seokpan-jenkins@users.noreply.github.com",
    ) -> None:
        if repo not in ALLOWED_REPOS:
            raise writer.TransportError("repository is not in the allowlist")
        if not user or not token:
            raise writer.TransportError("credential is missing")
        self._repo = repo
        self._owner = repo.split("/", 1)[0]
        self._user = user
        self._token = token
        self._checkout = Path(checkout)
        self._http = http or urllib_http
        self._git_bin = git_bin
        self._author = (author_name, author_email)

    def __repr__(self) -> str:  # never include the credential
        return f"GitHubApiTransport(repo={self._repo!r})"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        checkout: Path,
        repo: str = ALLOWED_REPOS[0],
        user_var: str = "GH_USER",
        token_var: str = "GH_TOKEN",
        **kwargs,
    ) -> "GitHubApiTransport":
        return cls(repo, environ.get(user_var, ""), environ.get(token_var, ""), checkout, **kwargs)

    # ------------------------------------------------------------ REST

    def _request(
        self,
        method: str,
        suffix: str,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping] = None,
    ) -> tuple:
        path = f"/repos/{self._repo}{suffix}"
        url = API_BASE + urllib.parse.quote(path, safe="/")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        if not url.startswith(API_BASE + "/"):
            raise writer.TransportError("refusing a request outside api.github.com")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._http(method, url, headers, data)
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
            if 200 <= status < 300:
                raise writer.TransportError(f"GitHub API {method} {suffix}: response is not JSON")
        return status, parsed

    def _fail(self, method: str, suffix: str, status: int, parsed: object) -> writer.TransportError:
        message = parsed.get("message") if isinstance(parsed, dict) else None
        detail = f": {writer.redact(message)[:200]}" if isinstance(message, str) else ""
        return writer.TransportError(f"GitHub API {method} {suffix} failed: HTTP {status}{detail}")

    @staticmethod
    def _pull(item: object) -> writer.PullRequestInfo:
        try:
            head = item["head"]["ref"]  # type: ignore[index]
            number = item["number"]  # type: ignore[index]
            state = item["state"]  # type: ignore[index]
            url = item["html_url"]  # type: ignore[index]
            merged = bool(item.get("merged_at"))  # type: ignore[union-attr]
        except (KeyError, TypeError, AttributeError):
            raise writer.TransportError("unexpected pull request response shape")
        if not isinstance(head, str) or not isinstance(number, int) or state not in ("open", "closed") or not isinstance(url, str):
            raise writer.TransportError("unexpected pull request response shape")
        return writer.PullRequestInfo(number, head, state, merged, url)

    def get_branch_sha(self, branch: str) -> Optional[str]:
        _check_branch(branch, promotion_only=False)
        suffix = f"/git/ref/heads/{branch}"
        status, parsed = self._request("GET", suffix)
        if status == 404:
            return None
        if status != 200:
            raise self._fail("GET", suffix, status, parsed)
        sha = parsed.get("object", {}).get("sha") if isinstance(parsed, dict) else None
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise writer.TransportError("unexpected branch response shape")
        return sha

    def find_pull_requests(self, head_branch: str) -> Sequence[writer.PullRequestInfo]:
        _check_branch(head_branch, promotion_only=False)
        suffix = "/pulls"
        status, parsed = self._request(
            "GET", suffix, query={"state": "all", "head": f"{self._owner}:{head_branch}", "per_page": str(PER_PAGE)}
        )
        if status != 200:
            raise self._fail("GET", suffix, status, parsed)
        if not isinstance(parsed, list):
            raise writer.TransportError("unexpected pulls response shape")
        if len(parsed) >= PER_PAGE:
            raise writer.TransportError("too many pull requests for one branch (not paginated)")
        return [pr for pr in (self._pull(item) for item in parsed) if pr.head_branch == head_branch]

    def list_open_pull_requests(self) -> Sequence[writer.PullRequestInfo]:
        suffix = "/pulls"
        found: list = []
        for page in range(1, MAX_PAGES + 1):
            status, parsed = self._request(
                "GET", suffix,
                query={"state": "open", "base": writer.BASE_BRANCH, "per_page": str(PER_PAGE), "page": str(page)},
            )
            if status != 200:
                raise self._fail("GET", suffix, status, parsed)
            if not isinstance(parsed, list):
                raise writer.TransportError("unexpected pulls response shape")
            found.extend(self._pull(item) for item in parsed)
            if len(parsed) < PER_PAGE:
                return found
        raise writer.TransportError("too many open pull requests (pagination limit reached)")

    def file_exists(self, path: str, ref: str) -> bool:
        clean = _check_path(path)
        if not (SHA_RE.match(ref) if isinstance(ref, str) else False):
            _check_branch(ref, promotion_only=False)
        suffix = f"/contents/{clean}"
        status, parsed = self._request("GET", suffix, query={"ref": ref})
        if status == 200:
            return True
        if status == 404:
            return False
        raise self._fail("GET", suffix, status, parsed)

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> str:
        _check_branch(head, promotion_only=True)
        if base != writer.BASE_BRANCH:
            raise writer.TransportError(f"pull requests may only target {writer.BASE_BRANCH}")
        suffix = "/pulls"
        status, parsed = self._request("POST", suffix, body={"title": title, "head": head, "base": base, "body": body})
        if status != 201:
            raise self._fail("POST", suffix, status, parsed)
        url = parsed.get("html_url") if isinstance(parsed, dict) else None
        if not isinstance(url, str):
            raise writer.TransportError("unexpected pull request response shape")
        return url

    def delete_branch(self, branch: str) -> None:
        _check_branch(branch, promotion_only=True)
        suffix = f"/git/refs/heads/{branch}"
        status, parsed = self._request("DELETE", suffix)
        if status != 204:
            raise self._fail("DELETE", suffix, status, parsed)

    # ------------------------------------------------------------ git

    def _git(self, args: Sequence[str], env: Optional[Mapping[str, str]] = None) -> str:
        run_env = dict(env) if env is not None else dict(os.environ)
        run_env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = subprocess.run(
                [self._git_bin, *args], cwd=self._checkout, env=run_env,
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise writer.TransportError(f"git {args[0]} could not run: {type(exc).__name__}")
        if proc.returncode != 0:
            raise writer.TransportError(
                f"git {args[0]} failed rc={proc.returncode}: {writer.redact(proc.stderr.strip())[:300]}"
            )
        return proc.stdout

    def push_commit(self, branch: str, base_sha: str, files: Mapping[str, str], message: str) -> str:
        _check_branch(branch, promotion_only=True)
        if not isinstance(base_sha, str) or not SHA_RE.match(base_sha):
            raise writer.TransportError("invalid base sha")
        if not files or len(files) > MAX_FILES:
            raise writer.TransportError("unexpected number of files")
        clean = {_check_path(path): text for path, text in files.items()}
        if any(not isinstance(text, str) for text in clean.values()):
            raise writer.TransportError("file content must be text")
        if not isinstance(message, str) or not message.strip():
            raise writer.TransportError("commit message is empty")

        if self._git(["rev-parse", "HEAD"]).strip() != base_sha:
            raise writer.TransportError("checkout HEAD differs from the base sha")
        if self._git(["status", "--porcelain"]).strip():
            raise writer.TransportError("checkout has uncommitted changes")
        self._git(["switch", "-c", branch])
        for path, text in clean.items():
            target = self._checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        changed = set(self._git(["diff", "--name-only"]).splitlines()) | set(
            self._git(["ls-files", "--others", "--exclude-standard"]).splitlines()
        )
        if changed != set(clean):
            raise writer.TransportError("changed files differ from the requested files")
        self._git(["add", "--", *sorted(clean)])
        name, email = self._author
        self._git(
            ["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
             "commit", "--no-verify", "-m", message]
        )
        commit_sha = self._git(["rev-parse", "HEAD"]).strip()

        with tempfile.TemporaryDirectory() as directory:
            askpass = make_askpass(Path(directory))
            env = dict(os.environ)
            env.update({"GIT_ASKPASS": str(askpass), USER_ENV: self._user, TOKEN_ENV: self._token})
            self._git(["push", "--no-verify", "origin", f"HEAD:refs/heads/{branch}"], env=env)
        return commit_sha
