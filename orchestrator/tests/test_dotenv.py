"""Phase4 收口：.env 配置加载（换设备不用重设环境变量）。

不引第三方依赖（python-dotenv 不在requirements里，为3行解析引入一个包不值）。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.utils import load_dotenv


def test_parses_basic_kv_and_comments(tmp_path):
    (tmp_path / ".env").write_text(
        "# 注释行\n"
        "ONLINE_MODEL=deepseek-v4-flash\n"
        "\n"
        "  ONLINE_BASE_URL = https://api.deepseek.com  \n"
        "EMPTY=\n", encoding="utf-8")
    got = load_dotenv(tmp_path)
    assert got["ONLINE_MODEL"] == "deepseek-v4-flash"
    assert got["ONLINE_BASE_URL"] == "https://api.deepseek.com"   # 两侧空格
    assert got["EMPTY"] == ""


def test_strips_quotes(tmp_path):
    (tmp_path / ".env").write_text(
        'A="带空格的 值"\nB=\'单引号\'\n', encoding="utf-8")
    got = load_dotenv(tmp_path)
    assert got["A"] == "带空格的 值"
    assert got["B"] == "单引号"


def test_does_not_override_real_env(tmp_path):
    """真实环境变量优先级更高——CI/临时调试要能盖过文件。"""
    os.environ["KEEP_ME"] = "from-env"
    try:
        (tmp_path / ".env").write_text("KEEP_ME=from-file\n", encoding="utf-8")
        load_dotenv(tmp_path)
        assert os.environ["KEEP_ME"] == "from-env"
        load_dotenv(tmp_path, override=True)
        assert os.environ["KEEP_ME"] == "from-file"
    finally:
        os.environ.pop("KEEP_ME", None)


def test_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path) == {}


def test_finds_orchestrator_and_root(tmp_path):
    """两处都找：orchestrator/.env 优先于仓库根/.env（配置与代码同侧）。"""
    root = tmp_path / "repo"
    orch = root / "orchestrator"
    orch.mkdir(parents=True)
    (root / ".env").write_text("WHICH=root\nONLY_ROOT=1\n", encoding="utf-8")
    (orch / ".env").write_text("WHICH=orch\n", encoding="utf-8")
    got = load_dotenv(orch)
    assert got["WHICH"] == "orch"
    assert got["ONLY_ROOT"] == "1"      # 根里独有的键也要加载


def test_shipped_env_example_is_valid():
    """仓库里的 .env.example 必须能被自己的解析器读懂（防止模板与解析器脱节）。"""
    root = Path(__file__).resolve().parents[1]
    ex = root / ".env.example"
    assert ex.exists(), ".env.example 缺失：换设备的人没模板"
    text = ex.read_text(encoding="utf-8")
    keys = [ln.split("=", 1)[0].strip() for ln in text.splitlines()
            if "=" in ln and not ln.strip().startswith("#")]
    for must in ("ONLINE_BASE_URL", "ONLINE_MODEL", "ONLINE_API_KEY",
                 "TAVILY_API_KEY"):
        assert must in keys, f".env.example 缺 {must}"
    # 模板里不能写真 key
    assert "sk-sk" not in text and "tvly-tvly" not in text


def test_real_env_is_gitignored():
    """真实 .env 绝不能进仓库——里面有 key。"""
    root = Path(__file__).resolve().parents[2]
    ign = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(l.strip() in (".env", ".env.local") for l in ign)
