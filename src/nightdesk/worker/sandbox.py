# src/nightdesk/worker/sandbox.py
"""bubblewrap argv construction with curated mount set.

Design goals (v1):

- Do NOT blanket-mount ``/etc`` — user-level secrets often land there
  (AWS creds, SSH config, daemon configs). Only specific files are
  mounted: ``resolv.conf``, ``hosts``, the system CA bundle, and the
  effectively-public ``passwd``/``group``.
- Mount ``/usr`` read-only because the claude binary, libc, and the
  dynamic linker all live there. The risk surface is low (system code,
  no per-user secrets).
- Mount the Python interpreter + venv ``site-packages`` + nightdesk
  package read-only so the SDK runner can import.
- Mount the resolved claude binary into the sandbox at a stable path.
- ``$HOME`` is a fresh tmpfs at ``/sandbox-home``; ``CLAUDE_CONFIG_DIR``
  points inside it. If the profile asked to inherit user credentials,
  only the single ``.credentials.json`` file is bind-mounted in.
- ``~/.config/nightdesk`` and ``~/.local/share/nightdesk`` are NEVER
  mounted under any condition. The sandboxed run has no path to the
  bearer token or secrets file on disk.

Network policy
--------------
The sandboxed CC needs network access to reach ``api.anthropic.com``
(or whatever ``ANTHROPIC_BASE_URL`` points at) for inference; without
that, no run can ever produce output. The host network namespace is
therefore always shared so inference works regardless of
``network_mode``. DNS + CA bundle are mounted unconditionally for the
same reason.

``network_mode`` survives as user intent and as the hook for future
per-host outbound enforcement (slirp4netns + allowlist, proxy, or
nftables in a CAP_NET_ADMIN helper). For v1 it does NOT block tool
network traffic at the namespace layer; that's enforced by
``denied_tools`` (Bash/WebFetch/etc.) instead.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from nightdesk.domain.permissions import PermissionSpec


log = logging.getLogger(__name__)


SANDBOX_HOME = "/sandbox-home"


def run_cc_sessions_dir(root: str, run_id: str) -> str:
    """Per-run host dir bound in as the sandbox's session store.

    Each run binds its own empty subdir as the sandbox's session store
    (CLAUDE_CONFIG_DIR/projects), so a run's agent can only ever see its own
    conversation — never another run's. After the run the session file is
    published to the host's real ~/.claude/projects so `claude --resume <id>`
    works.

    ``root`` MUST live outside the protected nightdesk data dir (see
    ``_exclusion_paths``) or the bind is refused — callers anchor it as a
    sibling of ``worktree_root`` (e.g. ~/.local/share/nightdesk-cc-sessions),
    exactly like worktrees live outside ~/.local/share/nightdesk so they can
    be bind-mounted.
    """
    return os.path.join(root, run_id)


def publish_cc_session(store_dir: str, session_id: str) -> Optional[str]:
    """Copy a sandboxed run's session file into the host ~/.claude/projects so
    interactive `claude --resume <id>` finds it.

    ``store_dir`` was bound in as CLAUDE_CONFIG_DIR/projects, so its layout
    mirrors ~/.claude/projects exactly (``<encoded-cwd>/<session-id>.jsonl``).
    We mirror the file's path-relative-to-store into the host store — no
    fragile cwd-encoding logic. Best-effort: never raises (a publish failure
    must not fail the run).
    """
    import glob

    try:
        matches = glob.glob(
            os.path.join(store_dir, "**", f"{session_id}.jsonl"), recursive=True
        )
        if not matches:
            return None
        src = matches[0]
        rel = os.path.relpath(src, store_dir)
        dst = os.path.join(os.path.expanduser("~/.claude/projects"), rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return dst
    except Exception:
        log.exception("failed to publish CC session %s from %s", session_id, store_dir)
        return None
# Keep this OUTSIDE any blanket ro mount (/usr, /opt). bwrap can't create the
# destination file inside an already-mounted read-only tree, so a path like
# /usr/local/bin/claude fails with "Read-only file system" the moment /usr
# is bound earlier in the argv.
SANDBOX_BIN_DIR = "/sandbox-bin"
SANDBOX_CLAUDE_BIN = f"{SANDBOX_BIN_DIR}/claude"

# Possible CA bundle locations across common distros, in order of preference.
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",        # Debian/Ubuntu/Alpine/Arch
    "/etc/pki/tls/certs/ca-bundle.crt",          # RHEL/Fedora/CentOS
    "/etc/ssl/ca-bundle.pem",                    # SUSE
    "/etc/ssl/cert.pem",                         # OpenBSD-style/macOS-style
)


def _resolve_ca_bundle() -> Optional[str]:
    for candidate in _CA_BUNDLE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _python_paths() -> list[str]:
    """Host paths that must be visible inside the sandbox for the SDK runner."""
    paths: set[str] = set()
    # Interpreter file itself + its prefix (lib/, include/ etc.).
    paths.add(sys.executable)
    paths.add(sys.prefix)
    if sys.base_prefix and sys.base_prefix != sys.prefix:
        paths.add(sys.base_prefix)
    # Standard library and platform-specific stdlib.
    paths.add(sysconfig.get_path("stdlib"))
    paths.add(sysconfig.get_path("platstdlib"))
    # Site-packages (where nightdesk and claude_agent_sdk live).
    for key in ("purelib", "platlib"):
        try:
            paths.add(sysconfig.get_path(key))
        except KeyError:
            pass
    # Also include each top-level package directory explicitly. Bind-mounting
    # the venv prefix usually covers these, but a non-venv install (e.g. uv
    # tool install) may put packages outside ``sys.prefix``.
    for mod_name in ("nightdesk", "claude_agent_sdk"):
        try:
            mod = __import__(mod_name)
            mod_file = getattr(mod, "__file__", None)
            if mod_file:
                paths.add(os.path.dirname(mod_file))
        except Exception:
            pass
    return [str(Path(p).resolve()) for p in paths if p]


def _dedup_under(paths: Iterable[str]) -> list[str]:
    """Drop paths that are already contained by another path in the set."""
    resolved = sorted({str(Path(p).resolve()) for p in paths if p}, key=len)
    kept: list[str] = []
    for p in resolved:
        if any(p == k or p.startswith(k.rstrip("/") + "/") for k in kept):
            continue
        kept.append(p)
    return kept


def _exclusion_paths() -> list[str]:
    """Host paths that must NEVER be visible inside the sandbox.

    bwrap doesn't have a deny-list primitive — exclusion is enforced by
    *not* mounting these and refusing to add a profile-supplied path that
    overlaps. This function is also used by the preflight validator.
    """
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "nightdesk"),
        os.path.join(home, ".local", "share", "nightdesk"),
        os.path.join(home, ".claude"),  # except the single creds file (see below)
    ]


def assert_no_excluded_paths(paths: Iterable[str]) -> None:
    excluded = [Path(p).resolve() for p in _exclusion_paths()]
    for p in paths:
        rp = Path(p).resolve()
        for ex in excluded:
            try:
                rp.relative_to(ex)
            except ValueError:
                continue
            raise ValueError(
                f"path {p!r} is inside protected directory {str(ex)!r}; "
                "refusing to bind-mount into sandbox"
            )


@dataclass
class _Mount:
    kind: str        # "ro-bind" | "bind"
    src: str
    dst: str


def _credential_mount(spec: PermissionSpec) -> Optional[_Mount]:
    creds = spec.claude_credentials or {}
    if creds.get("source") != "inherit":
        return None
    src = creds.get("value")
    if not isinstance(src, str) or not os.path.exists(src):
        raise ValueError(
            "profile claude_credentials.source='inherit' but the credentials "
            f"file is missing at {src!r}"
        )
    dst = f"{SANDBOX_HOME}/.claude/.credentials.json"
    return _Mount("ro-bind", src, dst)


def build_bwrap_argv(
    spec: PermissionSpec,
    *,
    cwd: str,
    cmd: list[str],
    env: dict[str, str],
    cc_sessions_dir: Optional[str] = None,
) -> list[str]:
    """Compose the bwrap invocation for a single sandboxed run.

    ``env`` is the full environment the sandboxed command receives; the
    caller is responsible for resolving secret references, injecting
    ``NIGHTDESK_RUN_TOKEN``, and applying the credentials env-var path
    (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``).
    """
    # Validate before assembling so the error message points at the cause.
    candidates = list(spec.fs_read) + list(spec.fs_write) + [cwd]
    assert_no_excluded_paths(candidates)

    cc_bin = spec.claude_binary_path or shutil.which("claude") or "/usr/local/bin/claude"
    log.info("build_bwrap_argv: cwd=%s claude_binary=%s fs_write=%d fs_read=%d",
             cwd, cc_bin, len(spec.fs_write), len(spec.fs_read))

    argv: list[str] = ["bwrap"]
    argv += ["--die-with-parent"]
    argv += ["--unshare-pid", "--unshare-uts", "--unshare-ipc"]
    # Network: always inherit the host's network namespace. ``--unshare-net``
    # would block ``api.anthropic.com``/``ANTHROPIC_BASE_URL`` and break
    # inference, so even ``network_mode="off"`` runs share net. The off-mode
    # restriction lives in ``denied_tools`` (Bash/WebFetch/etc.), not here.

    # Standard filesystem skeleton.
    argv += ["--proc", "/proc"]
    argv += ["--dev", "/dev"]
    argv += ["--tmpfs", "/tmp"]
    argv += ["--tmpfs", "/run"]
    argv += ["--tmpfs", SANDBOX_HOME]

    # /usr ro: needed for libc, the dynamic linker, system tools the claude
    # binary may exec. Not blanket-mounting /etc — only specific files below.
    if os.path.isdir("/usr"):
        argv += ["--ro-bind", "/usr", "/usr"]
    argv += ["--symlink", "usr/bin", "/bin"]
    argv += ["--symlink", "usr/lib", "/lib"]
    if os.path.isdir("/usr/lib64") or os.path.islink("/lib64"):
        argv += ["--symlink", "usr/lib64", "/lib64"]
    if os.path.isdir("/usr/sbin"):
        argv += ["--symlink", "usr/sbin", "/sbin"]
    if os.path.isdir("/opt"):
        # Some distros (and some node installs) park bins under /opt.
        argv += ["--ro-bind", "/opt", "/opt"]

    # Curated /etc: DNS + HTTPS trust. Mounted unconditionally so inference
    # (api.anthropic.com or ANTHROPIC_BASE_URL) works regardless of
    # ``network_mode``. Without resolv.conf, DNS fails; without the CA
    # bundle, every TLS handshake fails.
    if os.path.exists("/etc/resolv.conf"):
        argv += ["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"]
    if os.path.exists("/etc/hosts"):
        argv += ["--ro-bind", "/etc/hosts", "/etc/hosts"]
    ca = _resolve_ca_bundle()
    if ca:
        argv += ["--ro-bind", ca, ca]
        # Also expose the standard locations CC's HTTP client looks at.
        if ca != "/etc/ssl/certs/ca-certificates.crt":
            argv += ["--ro-bind", ca, "/etc/ssl/certs/ca-certificates.crt"]
    else:
        log.warning("no CA bundle found; TLS connections inside sandbox may fail")
    # If the host has /etc/passwd, expose it ro so name lookups (getpwuid,
    # logging, etc.) keep working. It's effectively public data on a Linux
    # system; we accept the minor info-leak rather than hand-rolling NSS.
    if os.path.exists("/etc/passwd"):
        argv += ["--ro-bind", "/etc/passwd", "/etc/passwd"]
    if os.path.exists("/etc/group"):
        argv += ["--ro-bind", "/etc/group", "/etc/group"]

    # Python runtime + venv + nightdesk + SDK. Drop anything already covered
    # by the /usr or /opt blanket mounts above; bwrap rejects double-mounting
    # the same destination.
    already_covered = ("/usr", "/opt")
    py_paths = [
        p for p in _dedup_under(_python_paths())
        if not any(p == c or p.startswith(c.rstrip("/") + "/") for c in already_covered)
    ]
    for p in py_paths:
        argv += ["--ro-bind-try", p, p]

    # Claude binary at a stable in-sandbox path.
    cc_bin = spec.claude_binary_path or shutil.which("claude") or "/usr/local/bin/claude"
    # If cc_bin is a symlink, resolve it first so the bind-mount points at
    # the real file (bwrap can't follow a dangling symlink at mount time).
    try:
        cc_bin_resolved = str(Path(cc_bin).resolve(strict=True))
    except FileNotFoundError as exc:
        raise ValueError(
            f"claude binary not found at {cc_bin!r}; configure profile.claude_binary_path "
            "or install claude into the daemon's PATH"
        ) from exc
    argv += ["--ro-bind", cc_bin_resolved, SANDBOX_CLAUDE_BIN]

    # Credentials (if inherit mode). Other modes pass via env.
    creds_mount = _credential_mount(spec)
    if creds_mount is not None:
        argv += [f"--{creds_mount.kind}", creds_mount.src, creds_mount.dst]

    # Per-run Claude session store: bind this run's own (empty) host dir over
    # the session store inside the otherwise-tmpfs home, so the conversation
    # survives sandbox teardown (and can be published for resume) WITHOUT
    # exposing the user's real ~/.claude or any other run's sessions.
    if cc_sessions_dir:
        # Fail loudly if a misconfigured store path overlaps protected
        # nightdesk dirs — never expose the data dir to the agent.
        assert_no_excluded_paths([cc_sessions_dir])
        os.makedirs(cc_sessions_dir, exist_ok=True)
        argv += ["--bind", cc_sessions_dir, f"{SANDBOX_HOME}/.claude/projects"]

    # User-declared filesystem allowances.
    for p in spec.fs_read:
        argv += ["--ro-bind-try", p, p]
    for p in spec.fs_write:
        argv += ["--bind-try", p, p]

    # Environment: clear and re-apply only what we explicitly want.
    argv += ["--clearenv"]
    for k, v in env.items():
        if v is None:
            continue
        argv += ["--setenv", k, str(v)]

    argv += ["--chdir", cwd]
    argv += ["--"]
    argv += cmd
    return argv
