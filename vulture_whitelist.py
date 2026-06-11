"""Vulture false-positive registry.

Each entry is a bare name reference that suppresses a vulture finding.
This file is parsed by vulture (via ``[tool.vulture]`` in pyproject.toml),
never imported or executed. Every entry carries the reason the symbol must
exist despite being "unused".
"""

# FastAPI binds the `/{rest_of_path:path}` catch-all route through this
# parameter name; the handler ignores the value by design (it always serves
# the patched index.html and lets React Router take over).
rest_of_path  # unused variable (src/theodosia/_ui.py:349)
