from .office import (
    read_docx,
    write_docx,
    read_xlsx,
    write_xlsx,
    read_pptx,
    write_pptx,
)
from .browser import (
    get_axtree,
    extract_text,
    click,
    fill,
    navigate,
    screenshot,
)

__all__ = [
    "read_docx",
    "write_docx",
    "read_xlsx",
    "write_xlsx",
    "read_pptx",
    "write_pptx",
    "get_axtree",
    "extract_text",
    "click",
    "fill",
    "navigate",
    "screenshot",
]
