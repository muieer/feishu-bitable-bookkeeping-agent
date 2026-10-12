import hashlib
import json
from copy import deepcopy

import pytest

from bookkeeping_agent.knowledge import Knowledge, KnowledgeError
from bookkeeping_agent.models import CategoryPath


def test_full_context_retains_complete_personal_knowledge_without_preferences(settings):
    rules = json.loads(settings.rules_path.read_text())
    rules["decision_policy"] = {"write_if_uncertain": False, "default_category": None}
    rules["rules"][0].update(
        boundary="赠礼不适用", historical_examples=["自己喝咖啡"], historical_count=3
    )
    settings.rules_path.write_text(json.dumps(rules))
    k = Knowledge(settings)
    original = deepcopy((k.categories, k.rules, k.schema))
    paths = (settings.categories_path, settings.rules_path, settings.schema_path)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    # Removed legacy preference files must never influence or prevent startup.
    (settings.rules_path.parent / "preferences.json").write_text("corrupt legacy data")
    context = json.loads(k.context())
    assert context["categories"] == k.categories and context["personal_rules"] == rules
    assert "explicit_preferences" not in context
    assert original == (k.categories, k.rules, k.schema)
    assert hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    assert not hasattr(k, "preferences") and not hasattr(k, "update_preference")


@pytest.mark.parametrize(
    "path",
    [
        {"level1": "饮食", "level2": "汽车", "level3": None},
        {"level1": "交通通信", "level2": "汽车", "level3": None},
        {"level1": "饮食", "level2": "咖啡", "level3": "充电"},
    ],
)
def test_full_category_path_only(settings, path):
    with pytest.raises(KnowledgeError):
        Knowledge(settings).validate_category(CategoryPath(**path))


def test_knowledge_version_mismatch_fails_startup(settings):
    rules = json.loads(settings.rules_path.read_text())
    rules["compatible_categories_version"] = "bad"
    settings.rules_path.write_text(json.dumps(rules))
    with pytest.raises(KnowledgeError):
        Knowledge(settings)
