# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# ADAPT of the install / uninstall / lock-file primitives from
# ``tools/skills_hub.py`` (© 2025 Nous Research, MIT).  Kept: the lock-file
# provenance model (``{version, installed:{name:{source, content_hash,
# install_path}}}``), the two-layer path-safety defence (walk component-by-
# component rejecting symlinks, then resolve + is_relative_to + !=root check),
# the ``bundle_content_hash`` logic, the "only remove what we installed" guard,
# and install_dir.rmtree-then-copy.  Dropped (Hermes coupling): quarantine stage,
# scan_verdict / trust_level / identifier fields, multi-source routing,
# audit_log, website policy, SSRF redirect chaining, provenance signing,
# telemetry.  Aegis uses a single skills_dir; there are no bundled or
# multi-profile skills dirs.
"""Skill installation core — install/uninstall/update skills.

:func:`install_skill` copies a local skill directory (or downloads a SKILL.md
from a URL) into the skills dir.  :func:`uninstall_skill` removes a
skill-gated by the lock file — only skills installed via this module can be
removed, so builtins and manually-placed skills stay untouched.

A small lock file (``<skills_dir>/.aegis-lock.json``) records the source and
content hash of each installed skill so that :func:`update_skill` can detect
upstream changes by comparing hashes.

Path safety (adapted from Hermes' ``_resolve_lock_install_path``):
  - The skill name is validated: no ``..``, no ``/``, no empty string.
  - The target directory is walked component-by-component; any symlink /
    junction redirect inside it is rejected.
  - After ``Path.resolve()``, the target must be inside the skills dir AND
    must NOT be the skills dir root itself (preventing ``rmtree(root)``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import httpx

from aegis_agent.skills.frontmatter import parse_frontmatter
from aegis_agent.skills.loader import MAX_NAME_LENGTH, SkillLoader

logger = logging.getLogger(__name__)

_LOCK_FILENAME = ".aegis-lock.json"

# -- lock file ---------------------------------------------------------------


class SkillLock:
    """Manages ``<skills_dir>/.aegis-lock.json`` — provenance of installed skills."""

    def __init__(self, skills_dir: Path) -> None:
        self._path = skills_dir / _LOCK_FILENAME

    def load(self) -> dict:
        if not self._path.exists():
            return {"version": 1, "installed": {}}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "installed": {}}

    def save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def get_installed(self, name: str) -> dict | None:
        return self.load()["installed"].get(name)

    def list_installed(self) -> list[dict]:
        data = self.load()
        return [{"name": name, **entry} for name, entry in data["installed"].items()]

    def record_install(self, name: str, source: str, install_rel: str, content_hash: str) -> None:
        data = self.load()
        data["installed"][name] = {
            "source": source,
            "install_path": install_rel,
            "content_hash": content_hash,
        }
        self.save(data)

    def record_uninstall(self, name: str) -> None:
        data = self.load()
        data["installed"].pop(name, None)
        self.save(data)

    def update_hash(self, name: str, content_hash: str, source: str) -> None:
        data = self.load()
        if name in data["installed"]:
            data["installed"][name]["content_hash"] = content_hash
            data["installed"][name]["source"] = source
        self.save(data)


# -- path safety --------------------------------------------------------------


def _valid_name(name: str) -> str:
    """Validate a skill name: non-empty, no ``..``, no ``/``, ≤MAX_NAME_LENGTH."""
    stripped = name.strip()
    if not stripped:
        raise ValueError("skill name must not be empty")
    if ".." in Path(stripped).parts:
        raise ValueError(f"skill name contains traversal: {stripped!r}")
    if "/" in stripped or "\\" in stripped:
        raise ValueError(f"skill name contains path separator: {stripped!r}")
    if len(stripped) > MAX_NAME_LENGTH:
        raise ValueError(f"skill name exceeds {MAX_NAME_LENGTH} chars")
    return stripped


def _is_redirect(path: Path) -> bool:
    """True when ``path`` is a symlink or (Windows) directory junction."""
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _resolve_install_path(skills_dir: Path, name: str) -> Path:
    """Resolve ``skills_dir / name`` with the two-layer Hermes safety defence.

    1. Walk component-by-component; reject symlink/junction redirects.
    2. After ``resolve()``, the target must be inside ``skills_dir`` AND must
       NOT be ``skills_dir`` itself (prevents ``rmtree(root)``).
    """
    skills_root = skills_dir.resolve()
    target = skills_dir
    parts = name.split("/")
    for part in parts:
        target = target / part
        if _is_redirect(target):
            raise ValueError(f"Unsafe install path (symlink/junction): {name!r}")
    target = target.resolve()
    if target == skills_root:
        raise ValueError(f"Unsafe install path (resolves to skills root): {name!r}")
    if not target.is_relative_to(skills_root):
        raise ValueError(f"Unsafe install path (escapes skills dir): {name!r}")
    return target


# -- content hash ------------------------------------------------------------


def _dir_hash(root: Path) -> str:
    """Content hash of all regular files under ``root``, sorted by relpath."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return f"sha256:{h.hexdigest()[:16]}"


# -- frontmatter read --------------------------------------------------------


def _read_skill_name(path: Path) -> str | None:
    """Extract the ``name`` from a SKILL.md file, or None."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frontmatter, _body = parse_frontmatter(content)
    raw = frontmatter.get("name", "")
    return str(raw).strip() if raw else None


# -- install / uninstall / update --------------------------------------------


def install_skill(
    source: str,
    skills_dir: Path,
    loader: SkillLoader,
    *,
    name: str | None = None,
    force: bool = False,
) -> dict:
    """Install a skill from a local directory or a remote SKILL.md URL.

    Returns ``{"success": True, "name": ..., "path": ...}`` or
    ``{"success": False, "error": ...}``.
    """
    # -- resolve source to a local directory with a SKILL.md ----------------
    if _is_url(source):
        return _install_from_url(source, skills_dir, loader, name=name, force=force)
    return _install_from_dir(source, skills_dir, loader, name=name, force=force)


def _install_from_dir(
    src: str,
    skills_dir: Path,
    loader: SkillLoader,
    *,
    name: str | None = None,
    force: bool = False,
) -> dict:
    src_path = Path(src).expanduser().resolve()
    if not src_path.is_dir():
        return {"success": False, "error": f"Source is not a directory: {src}"}
    skill_md = src_path / "SKILL.md"
    if not skill_md.is_file():
        return {"success": False, "error": f"No SKILL.md found in: {src}"}

    skill_name = name or _read_skill_name(skill_md)
    if not skill_name:
        return {"success": False, "error": "Could not determine skill name: missing or empty 'name' in SKILL.md frontmatter."}
    try:
        safe_name = _valid_name(skill_name)
    except ValueError as exc:
        return {"success": False, "error": f"Invalid skill name: {exc}"}

    # Reject symlinks inside the source tree.
    for entry in src_path.rglob("*"):
        if _is_redirect(entry):
            return {"success": False, "error": f"Skill source contains a symlink or junction: {entry.relative_to(src_path)}"}

    lock = SkillLock(skills_dir)
    existing = lock.get_installed(safe_name)
    if existing and not force:
        return {"success": False, "error": f"Skill {safe_name!r} is already installed. Use force=True to reinstall."}

    # Determine the install path relative to skills_dir (may include category).
    install_rel = _resolve_install_path(skills_dir, safe_name)

    if install_rel.exists():
        if not force:
            return {"success": False, "error": f"Directory already exists at {install_rel}. Use force=True to overwrite."}
        shutil.rmtree(install_rel)

    skills_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_path, install_rel)
    ch = _dir_hash(install_rel)
    lock.record_install(safe_name, src, safe_name, ch)
    loader.discover(force=True)
    return {"success": True, "name": safe_name, "path": str(install_rel)}


def _install_from_url(
    url: str,
    skills_dir: Path,
    loader: SkillLoader,
    *,
    name: str | None = None,
    force: bool = False,
) -> dict:
    """Download a SKILL.md from a URL and install it as a single-file skill."""
    try:
        resp = httpx.get(url, headers={"User-Agent": "aegis-agent/0.1"}, timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Failed to fetch {url}: {type(exc).__name__}: {exc}"}

    content = resp.text
    frontmatter, _body = parse_frontmatter(content)
    skill_name = name or str(frontmatter.get("name", "")).strip()
    if not skill_name:
        return {"success": False, "error": "Could not determine skill name from URL content."}
    try:
        safe_name = _valid_name(skill_name)
    except ValueError as exc:
        return {"success": False, "error": f"Invalid skill name: {exc}"}

    lock = SkillLock(skills_dir)
    existing = lock.get_installed(safe_name)
    if existing and not force:
        return {"success": False, "error": f"Skill {safe_name!r} is already installed. Use force=True to reinstall."}

    install_dir = _resolve_install_path(skills_dir, safe_name)
    if install_dir.exists():
        if not force:
            return {"success": False, "error": f"Directory already exists at {install_dir}. Use force=True to overwrite."}
        shutil.rmtree(install_dir)

    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "SKILL.md").write_text(content, encoding="utf-8")
    ch = _dir_hash(install_dir)
    lock.record_install(safe_name, url, safe_name, ch)
    loader.discover(force=True)
    return {"success": True, "name": safe_name, "path": str(install_dir)}


def uninstall_skill(name: str, skills_dir: Path, loader: SkillLoader) -> dict:
    """Remove a hub-installed skill (gated by the lock file).

    Returns ``{"success": True, "message": ...}`` or
    ``{"success": False, "error": ...}``.
    """
    try:
        safe_name = _valid_name(name)
    except ValueError as exc:
        return {"success": False, "error": f"Invalid skill name: {exc}"}

    lock = SkillLock(skills_dir)
    entry = lock.get_installed(safe_name)
    if not entry:
        return {"success": False, "error": f"{safe_name!r} is not installed (not tracked in lock file)."}

    try:
        install_path = _resolve_install_path(skills_dir, safe_name)
    except ValueError as exc:
        return {"success": False, "error": f"Refusing to uninstall {safe_name!r}: {exc}"}

    if install_path.exists():
        shutil.rmtree(install_path)

    lock.record_uninstall(safe_name)
    loader.discover(force=True)
    return {"success": True, "message": f"Uninstalled {safe_name!r}."}


def update_skill(name: str, skills_dir: Path, loader: SkillLoader) -> dict:
    """Check for an update by re-fetching the source and comparing hashes.

    Returns ``{"success": True, "status": "up_to_date"|"updated", ...}`` or
    ``{"success": False, "error": ...}``.
    """
    try:
        safe_name = _valid_name(name)
    except ValueError as exc:
        return {"success": False, "error": f"Invalid skill name: {exc}"}

    lock = SkillLock(skills_dir)
    entry = lock.get_installed(safe_name)
    if not entry:
        return {"success": False, "error": f"{safe_name!r} is not installed (not tracked in lock file)."}

    source = entry.get("source", "")
    current_hash = entry.get("content_hash", "")

    # re-fetch into a temp dir
    import tempfile
    tmp = tempfile.mkdtemp(prefix="aegis-update-")
    try:
        if _is_url(source):
            try:
                resp = httpx.get(source, headers={"User-Agent": "aegis-agent/0.1"}, timeout=30, follow_redirects=True)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"Failed to refetch {source}: {type(exc).__name__}: {exc}"}
            (Path(tmp) / "SKILL.md").write_text(resp.text, encoding="utf-8")
            latest_hash = _dir_hash(Path(tmp))
        else:
            src_path = Path(source).expanduser().resolve()
            if not src_path.is_dir():
                return {"success": False, "error": f"Source directory no longer exists: {source}"}
            # Hash the source directly so the key structure matches what
            # install_from_dir produces (both walk the same tree).
            latest_hash = _dir_hash(src_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if latest_hash == current_hash:
        return {"success": True, "status": "up_to_date", "name": safe_name}

    # re-install with force
    result = install_skill(source, skills_dir, loader, name=safe_name, force=True)
    if result.get("success"):
        result["status"] = "updated"
    return result


def list_installed(skills_dir: Path) -> list[dict]:
    """Return lock-file entries for installed hub skills."""
    return SkillLock(skills_dir).list_installed()


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


__all__ = [
    "SkillLock",
    "install_skill",
    "list_installed",
    "uninstall_skill",
    "update_skill",
]
