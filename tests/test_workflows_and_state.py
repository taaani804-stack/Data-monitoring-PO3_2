"""Workflow files and the state-branch script (real git, local bare remote)."""
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def _on(d):
    return d.get("on") or d.get(True)


def test_ranking_refresh_workflow_is_daily_serialised_with_the_scanner_and_uses_the_secret():
    wf = yaml.safe_load((WF / "ranking-refresh.yml").read_text())
    scan = yaml.safe_load((WF / "scanner.yml").read_text())
    assert _on(wf)["schedule"] == [{"cron": "40 23 * * *"}] and "workflow_dispatch" in _on(wf)
    assert wf["concurrency"]["group"] == scan["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False
    text = (WF / "ranking-refresh.yml").read_text()
    assert "secrets.COINMARKETCAP_API_KEY" in text and "--refresh-ranking" in text and "state_branch.sh save" in text
    assert "echo" not in text  # the key is only passed through env, never echoed


def test_hourly_scan_workflow_still_hourly_and_receives_the_cmc_secret():
    scan = yaml.safe_load((WF / "scanner.yml").read_text())
    assert _on(scan)["schedule"] == [{"cron": "5 * * * *"}]
    assert "secrets.COINMARKETCAP_API_KEY" in (WF / "scanner.yml").read_text()


def test_env_example_documents_both_secrets_without_values():
    text = (ROOT / ".env.example").read_text()
    for name in ("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID=", "COINMARKETCAP_API_KEY="):
        assert any(line.strip() == name for line in text.splitlines()), name


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_state_branch_script_persists_both_files_and_never_drops_remote_state(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    run = lambda *a, cwd=work, **k: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, **k)
    run("git", "init", "-q", "--bare", str(remote), cwd=tmp_path)
    run("git", "init", "-q", "-b", "main", str(work), cwd=tmp_path)
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    (work / "a").write_text("x"); run("git", "add", "a"); run("git", "commit", "-qm", "i")
    run("git", "remote", "add", "origin", str(remote)); run("git", "push", "-q", "origin", "main")
    shutil.copytree(ROOT / "scripts", work / "scripts")
    sh = lambda arg: run("bash", "scripts/state_branch.sh", arg)

    sh("load")  # no branch yet: fine
    (work / "state").mkdir(exist_ok=True)
    (work / "state" / "sent_signals.json").write_text('{"s": 1}')
    (work / "state" / "global_ranking.json").write_text('{"r": 1}')
    sh("save")
    shutil.rmtree(work / "state")
    sh("load")  # next workflow run
    assert (work / "state" / "sent_signals.json").read_text() == '{"s": 1}'
    assert (work / "state" / "global_ranking.json").read_text() == '{"r": 1}'
    # a run that only has the sent-signals file must not delete the cached ranking from the branch
    (work / "state" / "global_ranking.json").unlink()
    (work / "state" / "sent_signals.json").write_text('{"s": 2}')
    sh("save")
    show = lambda f: subprocess.run(["git", "--git-dir", str(remote), "show", f"scanner-state:{f}"], capture_output=True, text=True).stdout
    assert show("global_ranking.json") == '{"r": 1}' and show("sent_signals.json") == '{"s": 2}'
    assert run("git", "status", "--porcelain", "--untracked-files=no").stdout == ""  # working tree untouched
