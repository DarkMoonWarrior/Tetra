"""打包 Windows 桌面程序，一次生成两样东西：

- dist/Tetra-Setup-<版本>.exe：安装程序（Inno Setup）。安装时能选安装位置、只为自己装还是为所有用户装、
  桌面快捷方式、开机自启；装好以后在「设置 → 应用」里能卸载，卸载时问要不要连数据一起删。
- dist/Tetra-<版本>-windows-x64.zip：免安装版，解压到哪里都行，双击 Tetra.exe 就能用。

    .venv\\Scripts\\python -m pip install pyinstaller      （只要装一次，打包工具不写进 requirements.txt）
    winget install --id JRSoftware.InnoSetup -e --scope user   （做安装程序要用；没装就只出 zip）
    .venv\\Scripts\\python packaging\\build_windows.py

程序里不带任何 Key 和题库数据，模型在「个人设置 → 模型服务」里接入；数据存在 %LOCALAPPDATA%\\Tetra。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "packaging"
DIST = ROOT / "dist"


def find_iscc() -> Path | None:
    """Inno Setup 的命令行编译器：环境变量 INNO_SETUP 指定的，或者常见的安装位置。"""
    candidates = [os.environ.get("INNO_SETUP")]
    local = os.environ.get("LOCALAPPDATA")
    folders = [Path(local) / "Programs" if local else None, os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")]
    candidates += [str(Path(folder) / "Inno Setup 6" / "ISCC.exe") for folder in folders if folder]
    candidates.append(shutil.which("iscc"))
    return next((Path(p) for p in candidates if p and Path(p).exists()), None)


def build_installer(version: str, app: Path) -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        print("\n没找到 Inno Setup，这次只出免安装的 zip（装上 Inno Setup 6 再运行就会同时出安装程序）")
        return None
    messages = HERE / "ChineseSimplified.isl"
    if not messages.read_bytes().startswith(b"\xef\xbb\xbf"):
        raise SystemExit(f"{messages.name} 要存成带 BOM 的 UTF-8，不然安装程序里的中文会乱码")
    subprocess.run(
        [str(iscc), "/Qp", f"/DAppVersion={version}", f"/DSourceDir={app}", f"/DOutputDir={DIST}", str(HERE / "tetra.iss")],
        check=True, cwd=HERE,
    )
    return DIST / f"Tetra-Setup-{version}.exe"


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not (HERE / "tetra.ico").exists():
        subprocess.run([sys.executable, str(HERE / "make_icon.py")], check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
         "--workpath", str(ROOT / "build" / "pyinstaller"), str(HERE / "tetra.spec")],
        check=True, cwd=ROOT,
    )
    app = DIST / "Tetra"
    # 打包结果里绝不能有 Key 和用户数据
    leaked = [p for p in app.rglob("*") if p.name in (".env", "models.json", "profile.json", "desktop.json") or p.suffix == ".sqlite3"]
    if leaked:
        raise SystemExit(f"打包结果里有不该带的文件：{leaked}")
    shutil.copy(HERE / "使用说明.txt", app / "使用说明.txt")
    target = DIST / f"Tetra-{version}-windows-x64.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(app.rglob("*")):
            archive.write(path, Path("Tetra") / path.relative_to(app))
    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file())
    setup = build_installer(version, app)
    print(f"\n打包好了：{app / 'Tetra.exe'}（{size / 1e6:.0f} MB）")
    print(f"免安装版：{target}（{target.stat().st_size / 1e6:.0f} MB）")
    if setup and setup.exists():
        print(f"安装程序：{setup}（{setup.stat().st_size / 1e6:.0f} MB）")


if __name__ == "__main__":
    main()
