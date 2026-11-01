import copy
import json
from pathlib import Path
import sys


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "kotest"))
import decision_models as dm
import metrics
import route_answer as ra
import runtime

REFERENCE_DATE = "2026-10-22"
DATA_VERSION = "2026-11-02-api"
AGENTS = ("product", "order_delivery", "refund_exchange", "payment", "qna", "human")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def patched(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = patched(result.get(key, {}), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def leaves(value, prefix=""):
    if isinstance(value, dict):
        return {path: item for key, item in value.items() for path, item in leaves(item, prefix + ("." if prefix else "") + key).items()}
    return {prefix: value}


def at(value, path):
    for key in path.split("."):
        value = value[key]
    return value
