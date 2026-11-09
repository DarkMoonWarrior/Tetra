# PyInstaller 打包配置：生成 dist/Tetra/Tetra.exe（一个文件夹，依赖放在 _internal 里）。
# 用 packaging/build_windows.py 打包（它会先确认图标在、再打包、最后压缩成 zip），不要直接改这里的路径。
# 只带程序自带的文件（界面、知识树、示例要求、专题识别模型）；.env、data/runtime/（题库、设置、Key）一律不带。

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 提供

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "data" / "curriculum"), "data/curriculum"),
    (str(ROOT / "data" / "blueprint_specs.json"), "data"),
    (str(ROOT / "data" / "topic_model.json.gz"), "data"),
]
datas += collect_data_files("docx")  # Word 导出用的默认模板
datas += collect_data_files("latex2mathml")  # LaTeX → MathML 的符号表

hiddenimports = collect_submodules("mingti") + collect_submodules("uvicorn")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "tetra_desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "IPython", "matplotlib", "pandas", "scipy", "PyQt5", "PySide6", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Tetra",
    icon=str(ROOT / "packaging" / "tetra.ico"),
    console=False,  # 不要黑色的控制台窗口；日志写在 %LOCALAPPDATA%\Tetra\logs
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="Tetra",
    upx=False,
)
