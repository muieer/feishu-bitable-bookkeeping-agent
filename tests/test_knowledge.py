import hashlib
import json
from copy import deepcopy

import pytest
from test_service import preference

from bookkeeping_agent.knowledge import Knowledge, KnowledgeError
from bookkeeping_agent.models import CategoryPath


def test_full_knowledge_context_retains_rules_policy_and_constraints(settings):
    rules = json.loads(settings.rules_path.read_text())
    rules["decision_policy"] = {
        "priority": ["明确偏好", "规则", "历史"],
        "write_if_uncertain": False,
        "default_category": None,
    }
    rules["rules"][0].update(
        boundary="赠礼不适用", historical_examples=["自己喝咖啡"], historical_count=3
    )
    settings.rules_path.write_text(json.dumps(rules))
    k = Knowledge(settings)
    original = deepcopy((k.categories, k.rules, k.schema))
    hashes = [
        hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (settings.categories_path, settings.rules_path, settings.schema_path)
    ]
    context = json.loads(k.context())
    assert context["categories"] == k.categories and context["personal_rules"] == rules
    assert original == (k.categories, k.rules, k.schema)
    assert hashes == [
        hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (settings.categories_path, settings.rules_path, settings.schema_path)
    ]
    assert "明确的长期偏好优先于旧规则" in context["precedence"]


@pytest.mark.parametrize(
    "path",
    [
        {"level1": "饮食", "level2": "汽车"},
        {"level1": "交通通信", "level2": "汽车"},
        {"level1": "饮食", "level2": "咖啡", "level3": "充电"},
    ],
)
def test_full_category_path_only(settings, path):
    with pytest.raises(KnowledgeError):
        Knowledge(settings).validate_category(CategoryPath(**path))


def test_preferences_refresh_replace_subject_and_preserve_original_rules(settings):
    k = Knowledge(settings)
    rules = settings.rules_path.read_bytes()
    p = preference().preference
    k.update_preference(p)
    assert json.loads(k.context())["explicit_preferences"][0]["category"]["level3"] is None
    p.category = CategoryPath(level1="饮食", level2="咖啡")
    k.update_preference(p)
    assert len(k.preferences()) == 1 and k.preferences()[0].category.level2 == "咖啡"
    assert settings.rules_path.read_bytes() == rules


def test_knowledge_version_mismatch_and_corrupt_preferences_fail_startup(settings):
    rules = json.loads(settings.rules_path.read_text())
    rules["compatible_categories_version"] = "bad"
    settings.rules_path.write_text(json.dumps(rules))
    with pytest.raises(KnowledgeError):
        Knowledge(settings)
