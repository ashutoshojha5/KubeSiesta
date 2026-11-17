from functools import lru_cache

from app.config import get_settings
from app.kube import get_kube
from app.nodepools import get_nodepools
from app.scaler import Scaler
from app.state import State


@lru_cache
def get_scaler() -> Scaler:
    settings = get_settings()
    kube = get_kube(settings.label_prefix)
    return Scaler(kube, get_nodepools(), State(settings), settings)
