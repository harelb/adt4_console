"""The Hydra image store reader the console uses for images.

Hydra's own ``hydra_python.image_store`` (pure python, no compiled bindings
needed) first; else ``hydra_image_store``, the older Awesome-DCIST-T4 name for
the same package. Without either, nodes show no images (``optional``
feature ``image_store``).
"""

from __future__ import annotations

import importlib
import warnings

MODULES = ("hydra_python.image_store", "hydra_image_store")


def load():
    """The image store package; ImportError when there is none."""
    err = None
    for name in MODULES:
        try:
            with warnings.catch_warnings():
                # hydra_python warns when its compiled bindings are not built;
                # the image store does not need them
                warnings.simplefilter("ignore")
                return importlib.import_module(name)
        except ImportError as e:
            err = e
    raise ImportError(f"no Hydra image store reader ({', '.join(MODULES)}): {err}")


def submodule(name: str):
    """``<image store package>.<name>`` (e.g. ``indexer``, ``codec``)."""
    return importlib.import_module(f"{load().__name__}.{name}")


def available() -> bool:
    try:
        load()
    except ImportError:
        return False
    return True
