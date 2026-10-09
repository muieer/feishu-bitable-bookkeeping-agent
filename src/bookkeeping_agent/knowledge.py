import json
from pathlib import Path

from .config import Settings
from .models import CategoryPath, Preference
from .storage import atomic_json


class KnowledgeError(ValueError):
    pass


def read_system_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KnowledgeError(f"无法读取系统提示词文件：{path}。") from exc
    if not text:
        raise KnowledgeError(f"系统提示词文件为空：{path}。请放入正式提示词后启动 Agent。")
    return text


class Knowledge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.categories = json.loads(settings.categories_path.read_text(encoding="utf-8"))
        self.rules = json.loads(settings.rules_path.read_text(encoding="utf-8"))
        self.schema = json.loads(settings.schema_path.read_text(encoding="utf-8"))
        self.paths: set[tuple[str, str, str | None]] = set()
        for first in self.categories["tree"]:
            for second in first["children"]:
                for third in second["children"] or [{"name": None}]:
                    self.paths.add((first["name"], second["name"], third["name"]))
        if not self.paths:
            raise KnowledgeError("分类树为空。")
        if self.rules["compatible_categories_version"] != self.categories["version"]:
            raise KnowledgeError("分类规则与分类树版本不一致。")
        for rule in self.rules["rules"]:
            self.validate_category(
                CategoryPath(
                    **dict(zip(("level1", "level2", "level3"), rule["category"], strict=True))
                )
            )
        tables = [t for t in self.schema["tables"] if t["id"] == settings.bitable_table_id]
        if len(tables) != 1 or self.categories["table_id"] != settings.bitable_table_id:
            raise KnowledgeError("目标表与 schema.json / categories.json 不一致。")
        self.fields = tables[0]["fields"]
        self.validate_schema(self.fields)
        self.preferences()  # Fail at startup if persisted preferences are corrupt.

    def validate_category(self, category: CategoryPath) -> None:
        if (category.level1, category.level2, category.level3) not in self.paths:
            raise KnowledgeError("分类路径无效，请明确合法的一、二、三级分类。")

    def validate_schema(self, fields: list[dict]) -> None:
        s = self.settings
        indexed = {f["name"]: f for f in fields}
        expected = {
            s.bitable_date_field: "datetime",
            s.bitable_amount_field: "number",
            s.bitable_description_field: "text",
            **dict.fromkeys(s.category_fields, "select"),
        }
        if len(expected) != 6:
            raise KnowledgeError("写入字段名称不能重复。")
        for name, kind in expected.items():
            if name not in indexed or indexed[name]["type"] != kind:
                raise KnowledgeError(f"字段不存在或类型不符：{name}。")
        for level, name in enumerate(s.category_fields):
            field = indexed[name]
            if field.get("multiple") is not False:
                raise KnowledgeError(f"分类字段必须为单选：{name}。")
            allowed = {option["name"] for option in field["options"]}
            if any(path[level] is not None and path[level] not in allowed for path in self.paths):
                raise KnowledgeError(f"分类树包含表格中不存在的选项：{name}。")

    def preferences(self) -> list[Preference]:
        path = self.settings.preferences_path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"rules": []}
        rules = [Preference.model_validate(rule) for rule in data["rules"]]
        for rule in rules:
            self.validate_category(rule.category)
        return rules

    def update_preference(self, preference: Preference) -> None:
        self.validate_category(preference.category)
        rules = [rule for rule in self.preferences() if rule.subject != preference.subject]
        rules.append(preference)
        atomic_json(
            self.settings.preferences_path,
            {
                "version": 1,
                "rules": [rule.model_dump(mode="json") for rule in rules],
            },
        )

    def context(self) -> str:
        return json.dumps(
            {
                "categories": self.categories,
                "personal_rules": self.rules,
                "explicit_preferences": [p.model_dump(mode="json") for p in self.preferences()],
                "precedence": "明确的长期偏好优先于旧规则；明确规则优先于历史案例。",
            },
            ensure_ascii=False,
        )
