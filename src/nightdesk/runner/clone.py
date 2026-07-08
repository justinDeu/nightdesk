"""In-pod git clone: fetch the repo the run works against.

The pod has no host filesystem access, so it clones ``origin`` fresh and checks
out a run branch off ``base_ref`` — the remote-clone model from the design doc.
Non-git / remoteless workspaces are rejected host-side at preflight, so by the
time this runs the remote is known-good; failures here are genuine clone
failures and are surfaced as a ``CloneError`` (mapped to a workspace_error exit).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class CloneError(Exception):
    pass


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600,
    )


def clone_workspace(remote_url: str, base_ref: str, branch: str, dest: Path) -> str:
    """Clone ``remote_url``, check out ``base_ref``, branch ``branch`` off it.

    Returns the run-start commit SHA (HEAD after the branch is created), which
    the runner reports back so the run diff is computed from exactly this point.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _git("clone", "--no-single-branch", remote_url, str(dest))
    if r.returncode != 0:
        raise CloneError(f"git clone failed: {(r.stderr or '').strip()}")

    ref = base_ref or "HEAD"
    # Resolve base_ref: try origin/<ref> first (a pushed branch), then the ref
    # verbatim (a tag/sha), so both "main" and an explicit sha work.
    checkout = _git("checkout", f"origin/{ref}", cwd=str(dest))
    if checkout.returncode != 0:
        checkout = _git("checkout", ref, cwd=str(dest))
    if checkout.returncode != 0:
        raise CloneError(
            f"could not check out base_ref {ref!r}: {(checkout.stderr or '').strip()}"
        )

    b = _git("checkout", "-B", branch, cwd=str(dest))
    if b.returncode != 0:
        raise CloneError(f"could not create branch {branch!r}: {(b.stderr or '').strip()}")

    head = _git("rev-parse", "HEAD", cwd=str(dest))
    return (head.stdout or "").strip()
