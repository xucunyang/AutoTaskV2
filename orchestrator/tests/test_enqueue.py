import json
import time
from pathlib import Path

import pytest

from core import enqueue
from core.enqueue import EnqueueError, RateLimited
from core.store import Store


def _payload(tid: str = "t1", **over) -> dict:
    d = {
        "task_id": tid,
        "plan_id": "pipeline",
        "status": "PENDING",
        "idempotency_key": f"idem_{tid}",
        "objective": "test",
        "source": "pipeline",
        "priority": 5,
    }
    d.update(over)
    return d


def _write(root: Path, name: str, data: dict) -> Path:
    p = root / "inbox" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_enqueue_file_inserts_and_touches_wakeup(tmp_path):
    s = Store(tmp_path)
    f = _write(tmp_path, "a.json", _payload())
    status, row = enqueue.enqueue_file(s, f)
    assert status == "inserted"
    assert row["status"] == "PENDING"
    assert row["seq"] >= 1
    assert (tmp_path / "wakeup.flag").exists()


def test_idempotent_reenqueue_returns_already_exists(tmp_path):
    s = Store(tmp_path)
    f = _write(tmp_path, "a.json", _payload())
    enqueue.enqueue_file(s, f)
    status, row = enqueue.enqueue_file(s, f)
    assert status == "already_exists"
    assert row["task_id"] == "t1"
    assert len(s.list_by_status("PENDING")) == 1


def test_priority_override_and_fifo_order(tmp_path):
    s = Store(tmp_path)
    enqueue.enqueue_file(s, _write(tmp_path, "d1.json", _payload("d1", priority=10)))
    enqueue.enqueue_file(s, _write(tmp_path, "d2.json", _payload("d2", priority=10)))
    enqueue.enqueue_file(s, _write(tmp_path, "p1.json", _payload("p1")), priority=0)
    ready = []
    for t in s.list_by_status("PENDING"):
        ready.append(s.transition(t["plan_id"], t["task_id"], "READY", agent="a",
                                  run_id="r", expect_version=t["version"]))
    order = [t["task_id"] for t in s.list_ready_ordered(limit=10)]
    assert order == ["p1", "d1", "d2"]  # 高优插队 + 同优先级FIFO


def test_reject_oversize_file(tmp_path):
    s = Store(tmp_path)
    big = _payload("big", objective="x" * (101 * 1024))
    f = _write(tmp_path, "big.json", big)
    with pytest.raises(EnqueueError, match="file_too_large"):
        enqueue.enqueue_file(s, f)


def test_reject_bad_task_id(tmp_path):
    s = Store(tmp_path)
    for bad in ["T-Bad", "../evil", "a" * 65, "has space"]:
        f = _write(tmp_path, "bad.json", _payload(bad))
        with pytest.raises(EnqueueError, match="bad_task_id|schema_invalid"):
            enqueue.enqueue_file(s, f)


def test_reject_path_traversal_and_absolute(tmp_path):
    s = Store(tmp_path)
    d = "2026-09-29"
    bad_paths = ["../../../etc/passwd", "/etc/passwd", f"other/{d}/x.csv",
                 f"artifacts/{d}/../escape.csv"]
    for i, p in enumerate(bad_paths):
        payload = _payload(f"t{i}", acceptance=[{"type": "file_exists", "path": p}])
        f = _write(tmp_path, f"p{i}.json", payload)
        with pytest.raises(EnqueueError, match="path_"):
            enqueue.enqueue_file(s, f)
    good = _payload("ok", acceptance=[{"type": "file_exists",
                                        "path": f"artifacts/{d}/out.csv"}])
    f = _write(tmp_path, "ok.json", good)
    assert enqueue.enqueue_file(s, f)[0] == "inserted"


def test_reject_shell_metachar_in_cmd(tmp_path):
    s = Store(tmp_path)
    for i, cmd in enumerate(["rm -rf / ; echo x", "cat a | tee b", "echo `id`",
                             "curl x && y", "a > b", "a\nb"]):
        payload = _payload(f"c{i}", acceptance=[{"type": "python_test", "cmd": cmd}])
        f = _write(tmp_path, f"c{i}.json", payload)
        with pytest.raises(EnqueueError, match="shell_metachar|schema_invalid"):
            enqueue.enqueue_file(s, f)


def test_reject_business_rule_expr_injection(tmp_path):
    s = Store(tmp_path)
    bad_exprs = ["__import__('os').system('x')", "open('/etc/passwd')",
                 "a.b.c > 1", "[x for x in y]"]
    for i, e in enumerate(bad_exprs):
        payload = _payload(f"e{i}", acceptance=[
            {"type": "business_rule", "expr": e, "rule_id": f"r{i}"}])
        f = _write(tmp_path, f"e{i}.json", payload)
        with pytest.raises(EnqueueError, match="bad_expr_node|bad_expr_syntax|schema_invalid"):
            enqueue.enqueue_file(s, f)
    ok = _payload("eok", acceptance=[
        {"type": "business_rule", "expr": "n_rows > 100 and status == 'ok'",
         "rule_id": "r_ok"}])
    f = _write(tmp_path, "eok.json", ok)
    assert enqueue.enqueue_file(s, f)[0] == "inserted"


def test_reject_business_rule_without_expr(tmp_path):
    s = Store(tmp_path)
    payload = _payload("nr", acceptance=[{"type": "business_rule", "rule_id": "r"}])
    f = _write(tmp_path, "nr.json", payload)
    with pytest.raises(EnqueueError, match="business_rule_without_expr"):
        enqueue.enqueue_file(s, f)


def test_reject_needs_web_without_freshness(tmp_path):
    s = Store(tmp_path)
    f = _write(tmp_path, "w.json", _payload("w", needs_web=True, freshness="none"))
    with pytest.raises(EnqueueError, match="needs_web_requires_freshness"):
        enqueue.enqueue_file(s, f)
    ok = _write(tmp_path, "w2.json",
                _payload("w2", needs_web=True, freshness="recent"))
    assert enqueue.enqueue_file(s, ok)[0] == "inserted"


def test_rate_limit_blocks_after_100_per_hour(tmp_path):
    s = Store(tmp_path)
    log = tmp_path / "events" / "enqueue_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with log.open("w", encoding="utf-8") as f:
        for i in range(enqueue.RATE_LIMIT_PER_HOUR):
            f.write(json.dumps({"ts_epoch": now, "task_id": f"old{i}"}) + "\n")
    with pytest.raises(RateLimited):
        enqueue.validate(json.dumps(_payload()).encode("utf-8"), root=tmp_path, now=now)
    # 2小时前的记录不计入
    with log.open("w", encoding="utf-8") as f:
        for i in range(500):
            f.write(json.dumps({"ts_epoch": now - 7200, "task_id": f"old{i}"}) + "\n")
    assert enqueue.validate(json.dumps(_payload()).encode("utf-8"), root=tmp_path,
                            now=now)


def test_scan_inbox_moves_to_done_and_error(tmp_path):
    s = Store(tmp_path)
    _write(tmp_path, "good.json", _payload("good"))
    _write(tmp_path, "dup.json", _payload("dup"))
    _write(tmp_path, "bad.json", _payload("bad", acceptance=[
        {"type": "file_exists", "path": "/etc/passwd"}]))
    counts = enqueue.scan_inbox(s)
    assert counts == {"seen": 3, "inserted": 2, "already_exists": 0, "error": 1}
    assert (tmp_path / "inbox" / "done" / "good.json").exists()
    assert (tmp_path / "inbox" / "error" / "bad.json").exists()
    assert not list((tmp_path / "inbox").glob("*.json"))
    # 再投一次同名文件：done里的幂等键让入队返回already_exists
    _write(tmp_path, "good.json", _payload("good"))
    assert enqueue.scan_inbox(s)["already_exists"] == 1


def test_scan_inbox_survives_corrupt_json(tmp_path):
    s = Store(tmp_path)
    p = tmp_path / "inbox" / "corrupt.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    counts = enqueue.scan_inbox(s)
    assert counts["error"] == 1
    assert (tmp_path / "inbox" / "error" / "corrupt.json").exists()


def test_defaults_status_and_idempotency_key(tmp_path):
    """调用方不必写status/idempotency_key：入队通道补齐（与scheduler同派生规则）。"""
    s = Store(tmp_path)
    minimal = {"task_id": "min1", "objective": "o"}   # 只有这两个必填
    f = _write(tmp_path, "min.json", minimal)
    status, row = enqueue.enqueue_file(s, f)
    assert status == "inserted"
    assert row["status"] == "PENDING"
    assert row["idempotency_key"] == "pipeline/min1"   # Phase0§3：{plan}/{task}
    # 再投同一个 → 幂等命中
    assert enqueue.enqueue_file(s, f)[0] == "already_exists"
    # 显式给了idempotency_key就按给的算
    f2 = _write(tmp_path, "min2.json",
                {"task_id": "min2", "objective": "o", "idempotency_key": "custom"})
    _s2, row2 = enqueue.enqueue_file(s, f2)
    assert row2["idempotency_key"] == "custom"


def test_idempotency_key_includes_inputs_hash(tmp_path):
    """Phase0§3：键含inputs_hash全量哈希。同内容重投幂等；内容变了必须报冲突，
    不能静默复用旧任务（否则"换了输入还跑旧数据"永远查不出来）。"""
    s = Store(tmp_path)
    a = {"task_id": "dup", "objective": "o",
         "inputs": [{"path": "artifacts/2026-09-29/a.csv", "sha256": "s1"}]}
    f1 = _write(tmp_path, "i1.json", a)
    assert enqueue.enqueue_file(s, f1)[0] == "inserted"
    _st, row = enqueue.enqueue_file(s, f1)
    assert row["idempotency_key"].startswith("pipeline/dup/")
    assert len(row["idempotency_key"].split("/")[-1]) == 64   # 全hash不截断
    assert enqueue.enqueue_file(s, f1)[0] == "already_exists"
    # 同task_id不同inputs → 冲突
    b = dict(a, inputs=[{"path": "artifacts/2026-09-29/b.csv", "sha256": "s2"}])
    f2 = _write(tmp_path, "i2.json", b)
    with pytest.raises(EnqueueError, match="inputs_changed"):
        enqueue.enqueue_file(s, f2)


def test_shipped_pipeline_template_is_enqueueable(tmp_path):
    """回归：仓库自带的 templates/pipeline_task.json 必须真的能入队。
    历史问题：模板缺status/idempotency_key，CLI直接REJECTED。"""
    tpl = Path(__file__).resolve().parent.parent / "templates" / "pipeline_task.json"
    data = json.loads(tpl.read_text(encoding="utf-8"))
    data["task_id"] = "tpl_check"
    f = _write(tmp_path, "tpl.json", data)
    s = Store(tmp_path)
    status, row = enqueue.enqueue_file(s, f)
    assert status == "inserted"
    assert row["task_id"] == "tpl_check"
    # 管道模板必须自称pipeline：否则priority=0的管道任务会被当Daily统计
    assert row["source"] == "pipeline"
    assert row["priority"] == 0


def test_cli_rejects_bad_file_exit_code(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("core.enqueue.Store",
                        lambda *a, **k: Store(tmp_path))
    bad = _write(tmp_path, "cli.json", _payload("CLI-Bad"))
    assert enqueue.main(["--file", str(bad)]) == 2
    assert "REJECTED" in capsys.readouterr().out


def test_cli_inserts_ok(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("core.enqueue.Store",
                        lambda *a, **k: Store(tmp_path))
    ok = _write(tmp_path, "cli_ok.json", _payload("cli_ok"))
    assert enqueue.main(["--file", str(ok), "--priority", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "inserted" and out["priority"] == 0
