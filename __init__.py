"""ComfyUI-ColoristFilmGrain - colorist film tools for ComfyUI.

Two nodes, registered from one package:
  - Colorist Film Grain: 35mm-faithful film grain for video.
  - Colorist Halation: warm highlight bloom (run it before the grain).

Drop this folder into ComfyUI/custom_nodes and restart. Both nodes appear under
image/postprocessing. Torch only, no extra dependencies.
"""

from .colorist_film_grain import (
    NODE_CLASS_MAPPINGS as _GRAIN_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _GRAIN_NAMES,
)
from .colorist_halation import (
    NODE_CLASS_MAPPINGS as _HALATION_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _HALATION_NAMES,
)

NODE_CLASS_MAPPINGS = {**_GRAIN_CLASSES, **_HALATION_CLASSES}
NODE_DISPLAY_NAME_MAPPINGS = {**_GRAIN_NAMES, **_HALATION_NAMES}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
