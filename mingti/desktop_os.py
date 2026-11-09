"""桌面版和 Windows 打交道的部分（设置里"桌面版"那一页用）：

- 开机自启：注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 里的 Tetra 一项（安装程序勾"开机自启"写的也是它）；
  开机启动时带 --autostart，可以选窗口最小化。
- 快捷方式：桌面、开始菜单里的 Tetra.lnk（安装程序建的在同样的位置；为所有用户安装时在公共桌面，没有管理员权限删不掉）。
- 导出的 Word / PDF 存到哪：默认"文档\\Tetra"，可以换成别的文件夹，也可以每次都问。
- 数据文件夹：默认 %LOCALAPPDATA%\\Tetra\\data；换位置时记下来，下次启动 Tetra 时先把数据复制过去再用（见 apply_pending_move）。
- 选文件夹、另存为用系统的对话框（tkinter，单独开一个隐藏的窗口，放在最前面）。

偏好存在 <桌面数据目录>\\desktop.json，不放在数据文件夹里（数据文件夹本身的位置也记在这里）。
只在 Windows 的桌面程序里起作用；从源码运行时开机自启、快捷方式这些不可用（没有 Tetra.exe）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from mingti import config

log = logging.getLogger("mingti.desktop")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Tetra"
DEFAULTS = {
    "export_dir": "",  # 空：文档\Tetra
    "export_ask": False,  # 每次导出都问存到哪
    "autostart_minimized": True,  # 开机启动时窗口最小化
    "data_dir": "",  # 空：<桌面数据目录>\data
    "data_dir_next": "",  # 换了数据文件夹、还没搬：下次启动时搬
}
DATA_FILES = ("questions.sqlite3", "bank.sqlite3")  # 有这两个文件的文件夹就算是一份 Tetra 数据
_dialog_lock = threading.Lock()
WINDOWS = os.name == "nt"


def home() -> Path:
    """桌面版自己的目录：偏好、日志、窗口用的浏览器资料（不一定是数据文件夹）。"""
    return config.APP_HOME or config.DATA_DIR / "runtime" / "desktop"


def exe() -> str | None:
    """打包好的 Tetra.exe；从源码运行时是 None（开机自启、快捷方式都要指向它）。"""
    return sys.executable if config.FROZEN else None


def installed() -> bool:
    """是不是用安装程序装的（安装程序会在程序旁边放卸载程序）。"""
    return bool(exe()) and any(Path(sys.executable).parent.glob("unins*.exe"))


# ---------- 偏好 ----------


def load_prefs() -> dict:
    try:
        saved = json.loads((home() / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}


def save_prefs(changes: dict) -> dict:
    prefs = {**load_prefs(), **{key: value for key, value in changes.items() if key in DEFAULTS}}
    path = home() / "desktop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return prefs


# ---------- 系统文件夹 ----------


def _known_folder(csidl: int) -> Path | None:
    if not WINDOWS:
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer) != 0:
        return None
    return Path(buffer.value)


def documents() -> Path:
    return _known_folder(0x05) or Path.home() / "Documents"  # CSIDL_PERSONAL（跟着 OneDrive 的重定向走）


def export_dir(prefs: dict | None = None) -> Path:
    prefs = prefs or load_prefs()
    return Path(prefs["export_dir"]) if prefs.get("export_dir") else documents() / "Tetra"


def _shortcut_places() -> dict[str, tuple[Path | None, Path | None]]:
    """（当前用户的位置, 所有用户的位置）：桌面、开始菜单。"""
    return {
        "desktop": (_known_folder(0x10), _known_folder(0x19)),  # CSIDL_DESKTOPDIRECTORY / COMMON_DESKTOPDIRECTORY
        "start_menu": (_known_folder(0x02), _known_folder(0x17)),  # CSIDL_PROGRAMS / COMMON_PROGRAMS
    }


# ---------- 开机自启 ----------


def autostart_enabled() -> bool:
    if not WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return bool(str(value).strip())
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    target = exe()
    if not WINDOWS or not target:
        raise RuntimeError("开机自启只能在安装好的桌面版里设置")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{target}" --autostart')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---------- 快捷方式 ----------


def shortcuts() -> dict:
    """{desktop: 有没有, start_menu: 有没有, locked: [为所有用户建的、删不掉的]}。"""
    state, locked = {}, []
    for kind, (mine, everyone) in _shortcut_places().items():
        own = bool(mine and (mine / "Tetra.lnk").exists())
        common = bool(everyone and (everyone / "Tetra.lnk").exists())
        state[kind] = own or common
        if common and not own:
            locked.append(kind)
    return {**state, "locked": locked}


def set_shortcut(kind: str, on: bool) -> None:
    target = exe()
    if not WINDOWS or not target:
        raise RuntimeError("快捷方式只能在桌面版里设置")
    mine, everyone = _shortcut_places()[kind]
    if mine is None:
        raise RuntimeError("找不到这个位置")
    link = mine / "Tetra.lnk"
    if not on:
        link.unlink(missing_ok=True)
        if everyone and (everyone / "Tetra.lnk").exists():
            raise RuntimeError("这个快捷方式是安装时为所有用户建的，要用管理员权限才能删")
        return
    mine.mkdir(parents=True, exist_ok=True)
    quote = lambda value: str(value).replace("'", "''")  # noqa: E731 - PowerShell 单引号里的单引号写两个
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{quote(link)}'); $l.TargetPath = '{quote(target)}'; "
        f"$l.WorkingDirectory = '{quote(Path(target).parent)}'; $l.IconLocation = '{quote(target)},0'; "
        "$l.Description = 'Tetra 命题智能体'; $l.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        creationflags=0x08000000, timeout=30, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not link.exists():
        raise RuntimeError(f"快捷方式没建成：{(result.stderr or '').strip()[:200] or '原因不明'}")


# ---------- 对话框、资源管理器 ----------


def _tk_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # 放在 Tetra 窗口前面
    root.update()
    return root


def pick_folder(title: str, initial: Path | None = None) -> str | None:
    """系统的选文件夹对话框；点了取消返回 None。"""
    from tkinter import filedialog

    with _dialog_lock:
        root = _tk_root()
        try:
            chosen = filedialog.askdirectory(parent=root, title=title, initialdir=str(initial or documents()), mustexist=False)
        finally:
            root.destroy()
    return str(Path(chosen)) if chosen else None


def pick_save_file(title: str, folder: Path, name: str) -> str | None:
    """系统的另存为对话框；点了取消返回 None。"""
    from tkinter import filedialog

    suffix = Path(name).suffix.lower()
    types = {".docx": [("Word 文档", "*.docx")], ".pdf": [("PDF 文件", "*.pdf")]}.get(suffix, [])
    with _dialog_lock:
        root = _tk_root()
        try:
            chosen = filedialog.asksaveasfilename(
                parent=root, title=title, initialdir=str(folder), initialfile=name,
                defaultextension=suffix, filetypes=[*types, ("所有文件", "*.*")],
            )
        finally:
            root.destroy()
    return str(Path(chosen)) if chosen else None


def reveal(path: Path) -> None:
    """在资源管理器里打开所在文件夹，并选中这个文件。"""
    if WINDOWS:
        subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S603 - explorer 的 /select 只认这种写法
    else:
        open_path(path.parent)


def open_path(path: Path) -> None:
    """用系统默认的程序打开文件或文件夹。"""
    if WINDOWS:
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def unique_path(folder: Path, name: str) -> Path:
    """folder 里不重名的文件名："试卷.docx" 已经有了就用 "试卷（2）.docx"。"""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in name).strip().rstrip(".") or "试卷"
    target = folder / cleaned
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem}（{n}）{suffix}"
        n += 1
    return target


# ---------- 数据文件夹 ----------


def _is_data(folder: Path) -> bool:
    return all((folder / name).exists() for name in DATA_FILES)


def check_data_target(path: str) -> Path:
    """检查新的数据文件夹能不能用：能写；不是现在这个；要么是空的，要么本来就是一份 Tetra 数据（那就直接用它）。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("请选一个完整的文件夹路径")
    current = Path(config.RUNTIME_DIR).resolve()
    if target.resolve() == current:
        raise ValueError("这就是现在用的数据文件夹")
    if current in target.resolve().parents:
        raise ValueError("新文件夹不能放在现在的数据文件夹里面")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()) and not _is_data(target):
        raise ValueError("这个文件夹里已经有别的文件了：请选一个空文件夹，或者以前放过 Tetra 数据的文件夹")
    probe = target / ".tetra-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError("这个文件夹写不进去，换一个试试") from exc
    return target


def apply_pending_move() -> Path | None:
    """启动时（服务还没开数据库之前）：设置里换过数据文件夹，就把现在的数据复制过去，以后用新文件夹。
    新文件夹里本来就有 Tetra 数据时直接用它，不复制。旧文件夹原样留着（确认没问题以后可以自己删）。返回新的数据文件夹。"""
    prefs = load_prefs()
    pending = prefs.get("data_dir_next")
    if not pending:
        return None
    source, target = Path(config.RUNTIME_DIR), Path(pending)
    try:
        target.mkdir(parents=True, exist_ok=True)
        if not _is_data(target) and source.exists():
            for item in source.iterdir():
                destination = target / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
        save_prefs({"data_dir": str(target), "data_dir_next": ""})
        log.info("数据文件夹从 %s 换到了 %s", source, target)
        return target
    except OSError:
        log.exception("数据文件夹没搬成，还用原来的 %s", source)
        save_prefs({"data_dir_next": ""})
        return None
