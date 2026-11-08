"""Real subprocess exits at durable boundaries; every HTTP result is synthetic."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_overnight import execute
from test_x import Clock

from username_finder.overnight import OvernightStore
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_storage import XStore

CRASH_WORKER = r"""
import json,os,sys
from datetime import UTC,datetime,timedelta
from username_finder import roblox_protocol as rp,x
from username_finder.roblox import result_for
from username_finder.roblox_storage import RobloxStore
from username_finder.x_storage import XStore
from username_finder.storage import Store
from username_finder.overnight import OvernightStore,transport_failure
store=Store(sys.argv[1]);phase=sys.argv[2];scope=sys.argv[3]
at=datetime(2026,11,7,tzinfo=UTC)
clock=lambda:at
platforms=['Roblox','X'] if scope=='Both' else [scope]
identities={}
for platform in platforms:
    repo=RobloxStore(store) if platform=='Roblox' else XStore(store)
    delay=.5 if platform=='Roblox' else 1.5
    if platform=='Roblox':repo.set_preferred_interval(delay)
    else:
        repo.set_interval(delay)
        repo.set_connection({'mode':'proxy','proxy_url':'http://127.0.0.1:9'})
    names=['CrashOne'] if phase in ('last_commit','job_end') else ['CrashOne','CrashTwo','CrashThree']
    identity=repo.create_run(names) if platform=='Roblox' else repo.prepare(x.input_spec('\n'.join(names)))
    identities[platform]=identity
    night=OvernightStore(store,platform);night.enable(identity,True)
    repo.start(identity);night.begin(identity,clock=clock)
    if phase=='before_request':continue
    task=repo.claim_next(identity) if platform=='Roblox' else repo.claim(identity)
    night.requesting(task,clock=clock)
    if phase=='during_request':continue
    response=rp.Response(200,b'{"code":0,"message":"Username is valid"}',{'content-type':'application/json'})
    if platform=='X':
        response=x.protocol.Response(200,b'{"valid":true,"reason":"available"}',{'content-type':'application/json'})
    result=(result_for(task['username'],response,checked_at=at.isoformat()) if platform=='Roblox'
            else x.result_for(task['username'],response,checked_at=at.isoformat()))
    if phase=='after_response':continue
    if phase in ('recovery_wait','manual_stop','exhausted'):
        failure=transport_failure(type(response)(error='timeout',complete=False),TimeoutError())
        for index in range(4 if phase=='exhausted' else 1):
            if not night.retry(task,failure,at):break
            if phase=='exhausted':
                at=datetime.fromisoformat(night.get(identity)['retry_at'])
                task=repo.claim_next(identity) if platform=='Roblox' else repo.claim(identity)
                night.requesting(task,clock=clock)
        if phase=='manual_stop':night.stop(identity)
        continue
    if phase in ('during_persistence','during_commit'):
        def trace(sql):
            boundary=("SET state='Done'" in sql) if phase=='during_persistence' else sql=='COMMIT'
            if boundary:
                print(json.dumps(identities),flush=True)
                os._exit(23)
        store.db.set_trace_callback(trace)
    if platform=='Roblox':repo.finish_check(task,result,response_finished_at=at)
    else:repo.finish(task,result)
    if phase in ('committed','last_commit'):continue
    night.success(identity,clock=clock)
    if phase=='job_end':
        repo.end(identity,'Completed')
        continue
    if phase=='completed':
        while True:
            task=repo.claim_next(identity) if platform=='Roblox' else repo.claim(identity)
            if task is None:break
            at+=timedelta(seconds=2)
            result=(result_for(task['username'],response,checked_at=at.isoformat()) if platform=='Roblox'
                    else x.result_for(task['username'],response,checked_at=at.isoformat()))
            if platform=='Roblox':repo.finish_check(task,result,response_finished_at=at)
            else:repo.finish(task,result)
            night.success(identity,clock=clock)
        repo.end(identity,'Completed');night.end(identity,'Completed',clock=clock)
print(json.dumps(identities),flush=True)
os._exit(23)
"""


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize(
    "phase",
    [
        "before_request",
        "during_request",
        "after_response",
        "during_persistence",
        "during_commit",
        "committed",
        "last_commit",
        "job_end",
        "recovery_wait",
        "manual_stop",
        "exhausted",
        "completed",
    ],
)
def test_process_crash_boundaries_preserve_results_and_recovery_rules(tmp_path, platform, phase):
    path = tmp_path / "crash.db"
    child = subprocess.run(
        [sys.executable, "-c", CRASH_WORKER, str(path), phase, platform],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 23, child.stderr
    identity = json.loads(child.stdout)[platform]
    store = Store(path)
    try:
        repo = RobloxStore(store) if platform == "Roblox" else XStore(store)
        night = OvernightStore(store, platform)
        repo.recover()
        eligible = night.startup_candidates(repo)
        before = repo.results()[1]
        may_resume = phase in ("before_request", "committed", "recovery_wait")
        assert eligible == ([identity] if may_resume else [])
        if may_resume:
            rig = SimpleNamespace(
                store=store,
                repo=repo,
                night=night,
                identity=identity,
                platform=platform,
                clock=Clock(),
                calls=[],
            )
            execute(rig, automatic=True)
            assert len(rig.calls) == (2 if phase == "committed" else 3)
            assert repo.results()[1] == 3 and len(repo.history("CrashOne")) == 1
            if phase == "recovery_wait":
                assert rig.calls[0][1] == 30
                assert night.get(identity)["attempts"] == 1
            assert repo.progress(identity)["interval_seconds"] == (0.5 if platform == "Roblox" else 1.5)
            if platform == "X":
                assert repo.connection_for_run(identity) == {
                    "mode": "proxy",
                    "proxy_url": "http://127.0.0.1:9",
                }
        elif phase in ("last_commit", "job_end", "completed"):
            assert repo.progress(identity)["state"] == "Completed"
            assert night.get(identity)["state"] == "Completed"
            assert before == (3 if phase == "completed" else 1)
        elif phase == "manual_stop":
            assert before == 0 and night.get(identity)["state"] == "Stopped"
        else:
            assert before == 1 and repo.results()[0][0]["classification"] == "Unknown"
            assert night.get(identity)["state"] == "Review"
        old_count = repo.results()[1]
        repo.recover()
        night.startup_candidates(repo)
        assert repo.results()[1] == old_count
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        store.close()


@pytest.mark.parametrize("phase", ["during_request", "recovery_wait", "manual_stop", "committed"])
def test_both_platforms_survive_same_process_termination(tmp_path, phase):
    path = tmp_path / "both.db"
    child = subprocess.run(
        [sys.executable, "-c", CRASH_WORKER, str(path), phase, "Both"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 23, child.stderr
    identities = json.loads(child.stdout)
    store = Store(path)
    try:
        for platform, repo in (("Roblox", RobloxStore(store)), ("X", XStore(store))):
            repo.recover()
            night = OvernightStore(store, platform)
            eligible = night.startup_candidates(repo)
            assert bool(eligible) == (phase in ("recovery_wait", "committed"))
            if eligible:
                rig = SimpleNamespace(
                    store=store,
                    repo=repo,
                    night=night,
                    identity=identities[platform],
                    platform=platform,
                    clock=Clock(),
                    calls=[],
                )
                execute(rig, automatic=True)
                assert repo.results()[1] == 3
            elif phase == "manual_stop":
                assert night.get(identities[platform])["state"] == "Stopped"
            else:
                assert repo.results()[0][0]["classification"] == "Unknown"
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
