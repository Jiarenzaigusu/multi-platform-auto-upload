from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path.cwd()
datas = collect_data_files("patchright")
datas.append((str(project_root / "utils" / "stealth.min.js"), "utils"))
hiddenimports = (
    collect_submodules("local_agent")
    + collect_submodules("uploader")
    + collect_submodules("utils")
    + collect_submodules("patchright")
    + collect_submodules("pystray")
    + [
        "webapp.ai_copy.contracts",
        "webapp.ai_copy.errors",
        "webapp.ai_copy.product_lookup.cache",
        "webapp.ai_copy.product_lookup.interfaces",
        "webapp.ai_copy.product_lookup.public_http",
        "webapp.ai_copy.product_lookup.tmall_client",
        "webapp.ai_copy.product_lookup.tmall_reader",
        "webapp.workspaces.paths",
        "webapp.api.models",
        "webapp.api.browser_runtime",
        "webapp.api.platforms",
    ]
)

analysis = Analysis(
    [str(project_root / "local_agent" / "desktop.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "webapp.frontend",
        "webapp.api.main",
        "webapp.api.agent",
        "webapp.api.agent_tasks",
        "webapp.api.batch",
        "webapp.api.batch_jd",
        "webapp.api.batch_tmall",
        "webapp.api.media",
        "webapp.api.store",
        "webapp.api.tasks",
        "webapp.auth",
        "webapp.llm_adapter",
        "webapp.ai_copy.router",
        "webapp.ai_copy.service",
        "webapp.workspaces.registry",
        "webapp.workspaces.service",
        "pytest",
        "openpyxl",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MPAU-Agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
bundle = BUNDLE(
    COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="MPAU-Agent",
    ),
    name="MPAU Agent.app",
    bundle_identifier="com.mpau.agent",
)
