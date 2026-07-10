"""
模块分层重构助手 — 将 app/ 下的业务模块迁移到 app/services/ 子包
"""
import re
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parent.parent / "app"

SERVICES_FILES = [
    "email_fetcher.py",
    "llm_analyzer.py",
    "mail_forwarder.py",
    "ocr.py",
    "kb_client.py",
    "scheduler.py",
    "prompt_budget.py",
    "ocr_test_data.py",
]

def rewrite_file(filepath: Path, replacements: list[tuple[str, str]]):
    if not filepath.exists():
        print(f"  SKIP (not found): {filepath.name}")
        return
    content = filepath.read_text(encoding="utf-8")
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
    filepath.write_text(content, encoding="utf-8")

def update_intra_service_imports():
    apps_to_services = [
        ("from app.email_fetcher", "from app.services.email_fetcher"),
        ("from app.llm_analyzer", "from app.services.llm_analyzer"),
        ("from app.mail_forwarder", "from app.services.mail_forwarder"),
        ("from app.ocr import", "from app.services.ocr import"),
        ("from app.kb_client", "from app.services.kb_client"),
        ("from app.scheduler", "from app.services.scheduler"),
        ("from app.prompt_budget", "from app.services.prompt_budget"),
        ("from app.ocr_test_data", "from app.services.ocr_test_data"),
    ]
    for fname in SERVICES_FILES:
        fpath = BASE / fname
        if fpath.exists():
            rewrite_file(fpath, apps_to_services)
            print(f"  Updated imports in {fname}")

def move_files():
    services_dir = BASE / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    for fname in SERVICES_FILES:
        src = BASE / fname
        dst = services_dir / fname
        if src.exists():
            shutil.move(str(src), str(dst))
            print(f"  Moved {fname} -> app/services/{fname}")

def move_model_windows():
    src = BASE.parent / "config" / "model_windows.py"
    dst = BASE / "services" / "model_windows.py"
    if src.exists():
        shutil.move(str(src), str(dst))
        print(f"  Moved config/model_windows.py -> app/services/model_windows.py")
        config_dir = BASE.parent / "config"
        if config_dir.exists() and not list(config_dir.iterdir()):
            config_dir.rmdir()
            print(f"  Removed empty config/ directory")

    # Update references
    all_py = list(BASE.rglob("*.py")) + list((BASE.parent / "tests").rglob("*.py"))
    for fpath in all_py:
        if "__pycache__" in str(fpath) or ".venv" in str(fpath):
            continue
        rewrite_file(fpath, [("from config.model_windows", "from app.services.model_windows")])

def update_external_imports():
    apps_to_services = [
        ("from app.email_fetcher", "from app.services.email_fetcher"),
        ("from app.llm_analyzer", "from app.services.llm_analyzer"),
        ("from app.mail_forwarder", "from app.services.mail_forwarder"),
        ("from app.ocr import", "from app.services.ocr import"),
        ("from app.kb_client", "from app.services.kb_client"),
        ("from app.scheduler", "from app.services.scheduler"),
        ("from app.prompt_budget", "from app.services.prompt_budget"),
        ("from app.ocr_test_data", "from app.services.ocr_test_data"),
    ]
    external = [
        BASE / "main.py", BASE / "database.py", BASE / "auth.py",
        BASE.parent / "tests/test_kb_client.py",
        BASE.parent / "tests/test_llm_analyzer.py",
    ]
    routes_dir = BASE / "routes"
    for f in routes_dir.glob("*.py"):
        if f.name != "__init__.py":
            external.append(f)

    for fpath in external:
        if fpath.exists():
            rel = fpath.relative_to(BASE.parent)
            rewrite_file(fpath, apps_to_services)
            print(f"  Updated external: {rel}")

def delete_old_files():
    for fname in SERVICES_FILES:
        old = BASE / fname
        if old.exists():
            old.unlink()
            print(f"  Deleted old app/{fname}")

if __name__ == "__main__":
    print("Phase 2: Module restructuring")
    print("=" * 40)
    
    print("\nStep 1: Update intra-service imports...")
    update_intra_service_imports()
    
    print("\nStep 2: Move files to app/services/...")
    move_files()
    
    print("\nStep 3: Update external imports...")
    update_external_imports()
    
    print("\nStep 4: Move config/model_windows.py...")
    move_model_windows()
    
    print("\nStep 5: Delete old source files...")
    delete_old_files()
    
    print("\nDone! Now need to split app/config.py manually.")
