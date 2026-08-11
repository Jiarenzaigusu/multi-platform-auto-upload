from pathlib import Path

from utils.config import BASE_DIR

async def set_init_script(context):
    """Apply the shared browser script required by the two retained uploaders."""
    stealth_js_path = Path(BASE_DIR / "utils/stealth.min.js")
    await context.add_init_script(path=stealth_js_path)
    return context
