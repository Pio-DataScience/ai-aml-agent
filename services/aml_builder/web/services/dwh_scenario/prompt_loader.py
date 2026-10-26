"""Load versioned AML scenario generation prompts without import-time I/O."""

from pathlib import Path

_PROMPT_ROOT = Path(__file__).with_name("prompts")


def load_prompt(name: str) -> str:
    """Read a known versioned prompt on demand."""
    allowed = {"generator_v1.md", "critic_v1.md", "control_plane_v1.md"}
    if name not in allowed:
        raise ValueError("Unknown DWH scenario prompt")
    return (_PROMPT_ROOT / name).read_text(encoding="utf-8")
