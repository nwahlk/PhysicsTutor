from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).resolve().parent.parent / "data" / "catalog"
DEFAULT_CATALOG = CATALOG_DIR / "physics_b1.yaml"
DEFAULT_CARDS = CATALOG_DIR / "physics_b1_cards.yaml"


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeCard:
    definition: str
    law: str
    misconceptions: tuple[str, ...]
    example: str


@dataclass(frozen=True)
class KnowledgePoint:
    id: str
    name: str
    chapter_id: str
    section_id: str
    prereqs: tuple[str, ...] = ()
    card: KnowledgeCard | None = None


@dataclass
class Catalog:
    subject: str
    textbook: str
    tree: list[dict]
    points: dict[str, KnowledgePoint] = field(default_factory=dict)

    def get(self, kp_id: str) -> KnowledgePoint | None:
        return self.points.get(kp_id)


def _check_acyclic(points: dict[str, KnowledgePoint]) -> None:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(kp_id: str, path: list[str]) -> None:
        if kp_id in done:
            return
        if kp_id in visiting:
            raise CatalogError("前置关系成环: " + " -> ".join(path + [kp_id]))
        visiting.add(kp_id)
        for pre in points[kp_id].prereqs:
            visit(pre, path + [kp_id])
        visiting.discard(kp_id)
        done.add(kp_id)

    for kp_id in points:
        visit(kp_id, [])


def parse_catalog(raw: dict, cards: dict | None = None) -> Catalog:
    cards = cards or {}
    points: dict[str, KnowledgePoint] = {}
    seen: set[str] = set()

    def claim(node_id: str) -> str:
        if node_id in seen:
            raise CatalogError(f"重复的 id: {node_id}")
        seen.add(node_id)
        return node_id

    tree = []
    for chapter in raw["chapters"]:
        sections = []
        for section in chapter["sections"]:
            items = []
            for point in section["points"]:
                card_data = cards.get(point["id"])
                card = (
                    KnowledgeCard(
                        definition=card_data["definition"],
                        law=card_data["law"],
                        misconceptions=tuple(card_data["misconceptions"]),
                        example=card_data["example"],
                    )
                    if card_data
                    else None
                )
                kp = KnowledgePoint(
                    id=claim(point["id"]),
                    name=point["name"],
                    chapter_id=chapter["id"],
                    section_id=section["id"],
                    prereqs=tuple(point.get("prereqs", [])),
                    card=card,
                )
                points[kp.id] = kp
                items.append(
                    {"id": kp.id, "name": kp.name, "prereqs": list(kp.prereqs), "available": card is not None}
                )
            sections.append({"id": claim(section["id"]), "name": section["name"], "points": items})
        tree.append({"id": claim(chapter["id"]), "name": chapter["name"], "sections": sections})

    unknown_cards = set(cards) - set(points)
    if unknown_cards:
        raise CatalogError(f"卡片引用未知知识点: {', '.join(sorted(unknown_cards))}")
    for kp in points.values():
        for pre in kp.prereqs:
            if pre not in points:
                raise CatalogError(f"{kp.id} 的前置知识点 {pre} 不存在")
    _check_acyclic(points)
    return Catalog(subject=raw["subject"], textbook=raw["textbook"], tree=tree, points=points)


def load_catalog(path: Path = DEFAULT_CATALOG, cards_path: Path | None = DEFAULT_CARDS) -> Catalog:
    cards = yaml.safe_load(cards_path.read_text(encoding="utf-8")) if cards_path else {}
    return parse_catalog(yaml.safe_load(path.read_text(encoding="utf-8")), cards)
