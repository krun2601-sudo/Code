from __future__ import annotations

import importlib.util
from pathlib import Path


STAGE = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = STAGE / "algorithms" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_policy_installer_is_dry_run_first_and_round_trips(tmp_path: Path):
    install_module = load_script("install_to_platform")
    rollback_module = load_script("rollback_from_platform")
    platform = tmp_path / "platform"
    (platform / "zr10lab").mkdir(parents=True)
    (platform / "zr10lab" / "config.py").write_text("# marker\n", encoding="utf-8")

    preview = install_module.install(platform, apply=False, replace_existing=False)
    assert preview["dry_run"] is True
    assert not (platform / "algorithms").exists()

    installed = install_module.install(platform, apply=True, replace_existing=False)
    assert installed["dry_run"] is False
    assert (platform / "algorithms" / "user_policy.py").is_file()
    report = Path(installed["report_path"])

    rollback_preview = rollback_module.rollback(report, apply=False)
    assert rollback_preview["dry_run"] is True
    assert (platform / "algorithms" / "user_policy.py").is_file()

    rolled_back = rollback_module.rollback(report, apply=True)
    assert rolled_back["dry_run"] is False
    assert not (platform / "algorithms").exists()


def test_policy_installer_refuses_unmanaged_overwrite(tmp_path: Path):
    install_module = load_script("install_to_platform")
    platform = tmp_path / "platform"
    (platform / "zr10lab").mkdir(parents=True)
    (platform / "zr10lab" / "config.py").write_text("# marker\n", encoding="utf-8")
    (platform / "algorithms").mkdir()
    (platform / "algorithms" / "user_policy.py").write_text("# user file\n", encoding="utf-8")
    try:
        install_module.install(platform, apply=False, replace_existing=False)
    except ValueError as exc:
        assert "refusing to overwrite" in str(exc)
    else:
        raise AssertionError("unmanaged policy file was accepted for overwrite")
