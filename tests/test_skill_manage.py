"""skill_manage tool tests: install/uninstall/update/list (local dir; fake URL fetch)."""

from __future__ import annotations

import json
from pathlib import Path

from aegis_agent.skills.install import SkillLock
from aegis_agent.skills.loader import SkillLoader
from aegis_agent.skills.manage_tool import SkillManageTool


def _skill_dir(tmp_path, name="test-skill", desc="A test skill.", body="Body text."):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8"
    )
    return d


def _make_tool(skills_dir: Path) -> SkillManageTool:
    return SkillManageTool(SkillLoader([skills_dir]))


def test_install_from_local_dir(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "hello-world")
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "install", "source": str(src)}, None)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["name"] == "hello-world"
    # Lock file records it.
    lock = SkillLock(skills_dir)
    assert lock.get_installed("hello-world") is not None
    # Loader discovers it.
    loader = SkillLoader([skills_dir])
    assert loader.get("hello-world") is not None


def test_install_duplicate_refused(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "dup-skill")
    tool = _make_tool(skills_dir)
    assert json.loads(tool.run({"action": "install", "source": str(src)}, None).content)["success"] is True
    # Second install without force should be refused.
    result = tool.run({"action": "install", "source": str(src)}, None)
    assert result.is_error
    assert "already installed" in json.loads(result.content)["error"]


def test_install_force_overwrites(tmp_path):
    skills_dir = tmp_path / "skills"
    src1 = tmp_path / "force-v1"
    src1.mkdir()
    (src1 / "SKILL.md").write_text("---\nname: force-skill\ndescription: v1\n---\n\nv1\n", encoding="utf-8")
    src2 = tmp_path / "force-v2"
    src2.mkdir()
    (src2 / "SKILL.md").write_text("---\nname: force-skill\ndescription: v2\n---\n\nv2\n", encoding="utf-8")
    tool = _make_tool(skills_dir)
    tool.run({"action": "install", "source": str(src1)}, None)
    result = tool.run({"action": "install", "source": str(src2), "force": True}, None)
    payload = json.loads(result.content)
    assert payload["success"] is True
    # description updated to v2.
    installed = SkillLoader([skills_dir])
    installed.discover()
    assert installed.get("force-skill").description == "v2"


def test_uninstall(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "uninstall-me")
    tool = _make_tool(skills_dir)
    tool.run({"action": "install", "source": str(src)}, None)
    result = tool.run({"action": "uninstall", "name": "uninstall-me"}, None)
    payload = json.loads(result.content)
    assert payload["success"] is True
    # Lock cleared, loader no longer finds it.
    assert SkillLock(skills_dir).get_installed("uninstall-me") is None
    loader = SkillLoader([skills_dir])
    assert loader.get("uninstall-me") is None


def test_uninstall_not_installed(tmp_path):
    skills_dir = tmp_path / "skills"
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "uninstall", "name": "nope"}, None)
    assert result.is_error
    assert "not installed" in json.loads(result.content)["error"]


def test_update_no_source(tmp_path):
    skills_dir = tmp_path / "skills"
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "update", "name": "nope"}, None)
    assert result.is_error


def test_update_up_to_date(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "up-to-date")
    tool = _make_tool(skills_dir)
    tool.run({"action": "install", "source": str(src)}, None)
    result = tool.run({"action": "update", "name": "up-to-date"}, None)
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["status"] == "up_to_date"


def test_update_detects_change(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "stale-skill", body="old body")
    tool = _make_tool(skills_dir)
    tool.run({"action": "install", "source": str(src)}, None)
    # Change the source media.
    (src / "SKILL.md").write_text("---\nname: stale-skill\ndescription: desc\n---\n\nnew body\n", encoding="utf-8")
    result = tool.run({"action": "update", "name": "stale-skill"}, None)
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["status"] == "updated"
    # Content in installed dir reflects the new body.
    installed_dir = skills_dir / "stale-skill"
    assert "new body" in (installed_dir / "SKILL.md").read_text(encoding="utf-8")


def test_list_shows_installed(tmp_path):
    skills_dir = tmp_path / "skills"
    src = _skill_dir(tmp_path, "listable")
    tool = _make_tool(skills_dir)
    tool.run({"action": "install", "source": str(src)}, None)
    result = tool.run({"action": "list"}, None)
    payload = json.loads(result.content)
    names = [e["name"] for e in payload["installed"]]
    assert "listable" in names


def test_install_missing_source(tmp_path):
    skills_dir = tmp_path / "skills"
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "install"}, None)
    assert result.is_error
    assert "source" in json.loads(result.content)["error"]


def test_install_from_url(monkeypatch, tmp_path):
    skills_dir = tmp_path / "skills"

    def fake_get(url, **kwargs):
        class FakeResp:
            text = "---\nname: remote-skill\ndescription: A remote skill.\n---\n\nRemote body\n"
            def raise_for_status(self): pass
        return FakeResp()

    monkeypatch.setattr("aegis_agent.skills.install.httpx.get", fake_get)
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "install", "source": "https://example.com/skill.md"}, None)
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["name"] == "remote-skill"
    loader = SkillLoader([skills_dir])
    loader.discover()
    assert loader.get("remote-skill") is not None


def test_invalid_action(tmp_path):
    skills_dir = tmp_path / "skills"
    tool = _make_tool(skills_dir)
    result = tool.run({"action": "destroy"}, None)
    assert result.is_error
