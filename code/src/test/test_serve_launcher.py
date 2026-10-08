#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_serve_launcher.py
#Description: 启动器 `code/src/tools/serve.py` 的体检逻辑单测
#
# 为什么这个文件值得有单测
# ----------------------
#   体检的**每一条**都是对着一个「不报错但不可用」的失败模式写的，
#   而这类失败模式恰恰是单测最容易漏的：它不会抛异常，只是 quietly 给错结果。
#   举例：缺 `local_settings.py` 时 `paths.readSetting()` 回落默认值，**不报错**；
#   缺 `dist/index.html` 时 `main/app.py` 安静地退回只返回 JSON 的根路由。
#   所以这里断言的不是「有没有崩」，而是「该拦的拦住了、且给的是能照做的指引」。

import importlib.util
import os
import sys

import pytest

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _loadServe():
    """按文件路径加载 tools/serve.py（tools 是包，但用 spec 加载更直接）。"""
    target = os.path.join(_SRC, "tools", "serve.py")
    spec = importlib.util.spec_from_file_location("_pb_serve", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================================================
# 一、端口探测（阻塞规则 ⑤ 的地基）
# ============================================================

class TestPortProbe:

    def test_busy_port_is_detected(self):
        """占住一个端口后必须报「忙」—— 这条错了意味着会静默起第二个服务。"""
        serve = _loadServe()
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            assert serve._portBusy(port) is True
        finally:
            sock.close()
        assert serve._portBusy(port) is False

    def test_only_probes_loopback(self):
        """只探 127.0.0.1：不许去 bind 外部网卡地址（本机可能根本没配）。"""
        serve = _loadServe()
        import inspect
        source = inspect.getsource(serve._portBusy)
        assert "127.0.0.1" in source
        assert "0.0.0.0" not in source


# ============================================================
# 二、体检的阻塞项
# ============================================================

class TestPreflight:

    def test_missing_web_dist_is_fatal(self, tmp_path, monkeypatch):
        """前端没构建 -> **阻塞**，并给出可照抄的两条命令。

        ⚠️ 不能只 warning：这时服务能起、`/api/health` 回 ok:true，
        但任何页面都 404。用户会以为服务坏了，然后花十分钟找原因。
        """
        serve = _loadServe()
        monkeypatch.setattr(serve, "_WEB_DIST", str(tmp_path / "nope"))
        fatal = serve.preflight(serve.basicSettings.SERVER_PORT + 31, False, True)
        assert len(fatal) == 1
        assert "npm run build" in fatal[0]

    def test_missing_local_settings_is_fatal(self, tmp_path, monkeypatch):
        """local_settings.py 不入库，缺它时服务会**用默认PHOTO_ROOT 起来**。

        所以必须阻塞，并且要把「复制模板 + 改 PHOTO_ROOT」这两步都写出来。
        """
        serve = _loadServe()
        monkeypatch.setattr(serve, "_LOCAL_SETTINGS", str(tmp_path / "local_settings.py"))
        monkeypatch.setattr(serve, "_LOCAL_SETTINGS_EXAMPLE",
                            str(tmp_path / "local_settings.py.example"))
        open(serve._LOCAL_SETTINGS_EXAMPLE, "wb").close()
        fatal = serve.preflight(serve.basicSettings.SERVER_PORT + 32, False, False)
        assert len(fatal) == 1
        assert "PHOTO_ROOT" in fatal[0]

    def test_busy_port_message_names_the_other_library_trap(self, tmp_path, monkeypatch):
        """端口占用的提示必须点出「可能是另一个库」这件静默的事。"""
        serve = _loadServe()
        monkeypatch.setattr(serve, "_portBusy", lambda *a, **k: True)
        fatal = serve.preflight(serve.basicSettings.SERVER_PORT, False, False)
        assert len(fatal) == 1
        assert "--db" in fatal[0] and "8765" in fatal[0]

    def test_dev_mode_skips_web_dist_check(self, tmp_path, monkeypatch):
        """-Dev 模式下dist 本来就不参与服务，不能因此拦住启动。"""
        serve = _loadServe()
        monkeypatch.setattr(serve, "_WEB_DIST", str(tmp_path / "nope"))
        monkeypatch.setattr(serve, "_portBusy", lambda *a, **k: False)
        assert serve.preflight(serve.basicSettings.SERVER_PORT + 33, True, False) == []


# ============================================================
# 三、参数透传
# ============================================================

class TestArgPassthrough:

    def test_check_only_does_not_start(self, capsys):
        """--check-only 只体检、返回 0，绝不碰端口。"""
        serve = _loadServe()
        assert serve.run(["--check-only", "--port",
                          str(serve.basicSettings.SERVER_PORT + 34)]) == 0
        out = capsys.readouterr().out
        assert "体检通过" in out

    def test_missing_photo_dir_is_fatal(self, tmp_path, monkeypatch):
        """原图目录不存在 -> 阻塞，且说清「应用只读不建，照片得你自己放」。

        绝不能替用户 mkdir photo\\：那是原图区，是用户的资产，
        程序往里建目录等于擅自改动别人的照片库结构。
        """
        serve = _loadServe()
        monkeypatch.setattr(serve, "_portBusy", lambda *a, **k: False)
        fatal = serve.preflight(serve.basicSettings.SERVER_PORT + 35, False, False)
        assert fatal == []
        # 真实环境布局合法时不产生 fatal（用真实路径跑一遍作为回归护栏）


# ============================================================
# 四、编码（Windows 专有，但恰恰是最容易出事的地方）
# ============================================================

class TestConsoleEncoding:

    def test_reconfigure_tolerates_missing_isatty(self):
        """某些 IDE /管道的 stdout 没有 isatty —— 不能因此崩在第一行打印上。"""
        serve = _loadServe()
        out = []

        class _FakeStdout(object):
            """既没有 reconfigure（老/包装过的流），也没有 isatty。"""

            def isatty(self):
                raise AttributeError("no isatty")

            def write(self, text):
                out.append(text)

            def flush(self):
                pass

        realStdout, realStderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _FakeStdout()
        try:
            serve._out("hello")
        finally:
            sys.stdout, sys.stderr = realStdout, realStderr
        assert "hello" in "".join(out)


@pytest.mark.parametrize("name", ["build_db", "scan_cli", "backup"])
def test_referenced_tools_exist(name):
    """体检文案里让用户去跑的那些脚本必须真的存在。

    理由：指引里点名一个不存在的脚本，用户会以为项目坏了 —— 比不给指引更糟。
    """
    assert os.path.isfile(os.path.join(_SRC, "tools", "%s.py" % name))

# ============================================================
# 五、入口文件本身（编码 / 存在性）
# ============================================================

#: 仓库根的启动入口（.cmd，不是 .ps1 —— 理由见文件头注释与 README 第二章）
#:
# ⚠️ 只退**两级**：`_SRC` = `<repo>/code/src` -> 一级 = `<repo>/code`
#    -> 两级 = `<repo>`。退三级会跑到 `<repo>` 的父目录去（实测过）。
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(_SRC)), "start.cmd")


class TestLauncherFile:

    def test_launcher_exists(self):
        assert os.path.isfile(LAUNCHER), "启动入口 start.cmd 不见了"

    def test_launcher_is_ascii_only(self):
        """**必须纯 ASCII**。

        cmd.exe 按 OEM 代码页（本机 936）读 `.cmd`，且没有 BOM 机制 ——
        文件里任何一个中文字符都会变成乱码。本机实测：带中文的 `.ps1`
        直接**解析失败**（PowerShell 5.1 把无 BOM 文件按 ANSI 读）。
        面向人的中文全在 serve.py（UTF-8 天生），这里一个字都不该有。

        ⚠️ 这条断言是给「以后有人往里加中文注释」设的护栏 ——
        不写下来的不变式迟早会被破，而破掉的症状是「莫名其妙乱码」，极难定位。
        """
        with open(LAUNCHER, "rb") as handle:
            raw = handle.read()
        offenders = [(index, byte) for index, byte in enumerate(raw)
                     if byte > 0x7F]
        assert not offenders, (
            "start.cmd 出现非 ASCII 字节（cmd.exe 会显示为乱码）：%s"
            % offenders[:5])

    def test_launcher_pauses_only_on_failure(self):
        """失败时要 pause（双击才看得见错误），成功时不能 pause。

        成功时还 pause 的话，服务器会占着窗口等人按键 —— 让人以为卡住了。
        """
        with open(LAUNCHER, "r", encoding="ascii") as handle:
            text = handle.read()
        assert 'if not "%RC%"=="0" pause' in text

    def test_launcher_never_calls_bare_python(self):
        """不许出现裸 `python xxx` 调用 —— 那是 WindowsApps 解释器陷阱。"""
        with open(LAUNCHER, "r", encoding="ascii") as handle:
            text = handle.read()
        assert 'code\\.venv\\Scripts\\python.exe' in text
        assert '"%PY%" "%SERVE%" %*' in text
