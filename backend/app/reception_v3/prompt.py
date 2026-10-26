"""Load only the active company prompt."""
from app.business_profile import read_text

SYSTEM_PROMPT = read_text("system-prompt.txt")
