"""Measurements and reports of the extraction evaluation (`wikipulse.eval.extraction`).

Pure functions: the checks against Wikidata's answers and the labels, their counts, and
the report as JSON and Markdown. Nothing here reads the network or a database.
"""

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wikipulse.eval.extraction_data import ExtractionData, RelationView
from wikipulse.eval.relation_labels import RelationKey, RelationLabelRecord
from wikipulse.eval.wikidata import Statement

ENTITY_TYPES = ("person", "organization", "place", "event", "other")
LINK_METHODS = ("page", "link", "search", "none")


# --- 1. Linking accuracy ------------------------------------------------------------


@dataclass(frozen=True)
class LinkCheck:
    """One name the model used for an entity, against the items carrying it as a label."""

    entity_key: str
    name: str
    type: str
    link_method: str
    linked_id: str | None
    candidates: tuple[str, ...]  # items whose Wikidata label is the name

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def outcome(self) -> str:
        """no_label, unlinked, correct (linked to a candidate) or wrong."""
        if not self.candidates:
            return "no_label"
        if self.linked_id is None:
            return "unlinked"
        return "correct" if self.linked_id in self.candidates else "wrong"


def entity_names(data: ExtractionData) -> list[tuple[str, str]]:
    """(entity key, name) for every name the model used, each pair once."""
    return sorted({(key, alias) for key, e in data.entities.items() for alias in e.aliases})


def check_links(
    data: ExtractionData, items_by_label: Mapping[str, tuple[str, ...]]
) -> list[LinkCheck]:
    """Each linked name checked against the Wikidata items carrying it."""
    checks = []
    for key, name in entity_names(data):
        entity = data.entities[key]
        candidates = items_by_label.get(" ".join(name.split()), ())
        checks.append(
            LinkCheck(key, name, entity.type, entity.link_method, entity.wikidata_id, candidates)
        )
    return checks


@dataclass
class LinkCounts:
    """Outcomes of a group of link checks."""

    names: int = 0
    no_label: int = 0
    # Names exactly one item carries: the ground truth is that item.
    unique_correct: int = 0
    unique_wrong: int = 0
    unique_unlinked: int = 0
    # Names several items carry: only "linked to one of them" can be checked.
    shared_candidate: int = 0
    shared_other: int = 0
    shared_unlinked: int = 0

    def add(self, check: LinkCheck) -> None:
        self.names += 1
        outcome = check.outcome
        if outcome == "no_label":
            self.no_label += 1
            return
        if check.ambiguous:
            name = {"correct": "shared_candidate", "wrong": "shared_other"}.get(
                outcome, "shared_unlinked"
            )
        else:
            name = f"unique_{outcome}"
        setattr(self, name, getattr(self, name) + 1)

    @property
    def unique_linked(self) -> int:
        return self.unique_correct + self.unique_wrong

    @property
    def accuracy(self) -> float | None:
        """Linking accuracy: of linked names with exactly one labeled item, how many
        point to that item."""
        return ratio(self.unique_correct, self.unique_linked)

    @property
    def shared_rate(self) -> float | None:
        """Of linked names several items carry, how many point to one of them."""
        return ratio(self.shared_candidate, self.shared_candidate + self.shared_other)

    def as_dict(self) -> dict[str, Any]:
        return {
            "names": self.names,
            "no_label": self.no_label,
            "unique_correct": self.unique_correct,
            "unique_wrong": self.unique_wrong,
            "unique_unlinked": self.unique_unlinked,
            "shared_candidate": self.shared_candidate,
            "shared_other": self.shared_other,
            "shared_unlinked": self.shared_unlinked,
            "accuracy": self.accuracy,
            "accuracy_margin_95": margin(self.accuracy, self.unique_linked),
            "shared_rate": self.shared_rate,
        }


def count_links(checks: Iterable[LinkCheck]) -> LinkCounts:
    """The link checks added up."""
    counts = LinkCounts()
    for check in checks:
        counts.add(check)
    return counts


# --- 2. Relation support ------------------------------------------------------------


@dataclass(frozen=True)
class SupportCheck:
    """One relationship, and the Wikidata statements between its two entities."""

    relation: RelationView
    subject_id: str | None
    object_id: str | None
    statements: tuple[Statement, ...] = ()

    @property
    def both_linked(self) -> bool:
        return self.subject_id is not None and self.object_id is not None

    @property
    def supported(self) -> bool:
        return bool(self.statements)


def linked_pairs(data: ExtractionData) -> list[tuple[str, str]]:
    """(subject ID, object ID) of every relationship between two linked entities."""
    pairs = set()
    for relation in data.relations:
        a = data.entities[relation.subject].wikidata_id
        b = data.entities[relation.object].wikidata_id
        if a and b and a != b:
            pairs.add((a, b))
    return sorted(pairs)


def check_support(
    data: ExtractionData, statements: Mapping[frozenset[str], Sequence[Statement]]
) -> list[SupportCheck]:
    """Each relation checked for a Wikidata statement between its entities."""
    checks = []
    for relation in data.relations:
        a = data.entities[relation.subject].wikidata_id
        b = data.entities[relation.object].wikidata_id
        found = tuple(statements.get(frozenset((a, b)), ())) if a and b else ()
        checks.append(SupportCheck(relation, a, b, found))
    return checks


# --- 3. Labeled precision -----------------------------------------------------------


@dataclass
class LabelCounts:
    sampled: int = 0
    correct: int = 0
    wrong: int = 0
    unsure: int = 0
    labelers: Counter = field(default_factory=Counter)

    def add(self, label: RelationLabelRecord | None) -> None:
        self.sampled += 1
        if label is None:
            return
        setattr(self, label.label, getattr(self, label.label) + 1)
        self.labelers[labeler_name(label)] += 1

    @property
    def unlabeled(self) -> int:
        return self.sampled - self.correct - self.wrong - self.unsure

    @property
    def precision(self) -> float | None:
        return ratio(self.correct, self.correct + self.wrong)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sampled": self.sampled,
            "correct": self.correct,
            "wrong": self.wrong,
            "unsure": self.unsure,
            "unlabeled": self.unlabeled,
            "precision": self.precision,
            "precision_margin_95": margin(self.precision, self.correct + self.wrong),
            "labelers": dict(sorted(self.labelers.items())),
        }


def labeler_name(label: RelationLabelRecord) -> str:
    """Who made a relation label, for the report."""
    if label.labeler == "human":
        return "human"
    return f"llm ({label.model}, judge prompt v{label.prompt_version})"


def count_labels(
    sample: Sequence[RelationView], labels: Mapping[RelationKey, RelationLabelRecord]
) -> LabelCounts:
    """The labels of the sampled relations added up."""
    counts = LabelCounts()
    for relation in sample:
        counts.add(labels.get(relation.key))
    return counts


# --- Helpers ------------------------------------------------------------------------


def ratio(part: int, whole: int) -> float | None:
    """`part / whole`, or None when `whole` is 0."""
    return part / whole if whole else None


def margin(p: float | None, n: int) -> float | None:
    """Half-width of a 95% normal interval around a share `p` of `n`."""
    return None if p is None or not n else 1.96 * math.sqrt(p * (1 - p) / n)


# --- The report ---------------------------------------------------------------------


def by_key(items: Iterable[Any], key: Any) -> dict[str, list[Any]]:
    """`items` grouped by `key(item)`."""
    grouped: dict[str, list[Any]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def ordered(names: Iterable[str], known: Sequence[str]) -> list[str]:
    """Known names in their fixed order, then any others alphabetically."""
    names = set(names)
    return [n for n in known if n in names] + sorted(names - set(known))


def overview(data: ExtractionData) -> dict[str, Any]:
    """Counts of events, entities, links and relations of the extraction."""
    entities = list(data.entities.values())
    methods = Counter(e.link_method for e in entities)
    linked = sum(e.wikidata_id is not None for e in entities)
    both = sum(
        data.entities[r.subject].wikidata_id is not None
        and data.entities[r.object].wikidata_id is not None
        for r in data.relations
    )
    return {
        "events": len(data.events),
        "entities": len(entities),
        "entity_mentions": sum(len(keys) for keys in data.mentions.values()),
        "linked": linked,
        "linked_share": ratio(linked, len(entities)),
        "by_link_method": {m: methods.get(m, 0) for m in ordered(methods, LINK_METHODS)},
        "by_type": dict(Counter(e.type for e in entities).most_common()),
        "relations": len(data.relations),
        "relations_both_linked": both,
    }


def support_summary(checks: Sequence[SupportCheck]) -> dict[str, Any]:
    """The Wikidata support rate, matching properties and examples."""
    linked = [c for c in checks if c.both_linked]
    supported = [c for c in linked if c.supported]
    properties = Counter(
        f"{s.property} {s.label}"
        for c in supported
        for s in {(s.property, s.label): s for s in c.statements}.values()
    )
    return {
        "relations": len(checks),
        "both_linked": len(linked),
        "supported": len(supported),
        "support_rate": ratio(len(supported), len(linked)),
        "support_margin_95": margin(ratio(len(supported), len(linked)), len(linked)),
        "properties": dict(properties.most_common(15)),
    }


def type_rows(
    data: ExtractionData,
    link_checks: Sequence[LinkCheck],
    support_checks: Sequence[SupportCheck],
    sample: Sequence[RelationView],
    labels: Mapping[RelationKey, RelationLabelRecord],
) -> dict[str, dict[str, Any]]:
    """Per entity type (relations count under their subject's type)."""

    def subject_type(relation: RelationView) -> str:
        return data.entities[relation.subject].type

    entities = by_key(data.entities.values(), lambda e: e.type)
    links = by_key(link_checks, lambda c: c.type)
    support = by_key(support_checks, lambda c: subject_type(c.relation))
    sampled = by_key(sample, subject_type)
    rows = {}
    for name in ordered(set(entities) | set(support), ENTITY_TYPES):
        members = entities.get(name, [])
        linked = sum(e.wikidata_id is not None for e in members)
        rows[name] = {
            "entities": len(members),
            "linked": linked,
            "linked_share": ratio(linked, len(members)),
            "linking": count_links(links.get(name, [])).as_dict(),
            "support": support_summary(support.get(name, [])),
            "labels": count_labels(sampled.get(name, []), labels).as_dict(),
        }
    return rows


def entity_label(data: ExtractionData, key: str) -> str:
    """An entity's name, with its Wikidata id if linked."""
    entity = data.entities[key]
    return f"{entity.name} ({entity.wikidata_id})" if entity.wikidata_id else entity.name


def sample_rows(
    data: ExtractionData,
    sample: Sequence[RelationView],
    labels: Mapping[RelationKey, RelationLabelRecord],
    support: Mapping[RelationKey, SupportCheck],
) -> list[dict[str, Any]]:
    """The labeled sample's rows for the report."""
    rows = []
    for relation in sample:
        label = labels.get(relation.key)
        check = support.get(relation.key)
        rows.append(
            {
                "event": relation.event_key,
                "subject": entity_label(data, relation.subject),
                "relation": relation.relation,
                "object": entity_label(data, relation.object),
                "subject_type": data.entities[relation.subject].type,
                "source_title": relation.source_title,
                "source_revision": relation.source_revision,
                "label": label.label if label else None,
                "labeler": labeler_name(label) if label else None,
                "reason": label.reason if label else "",
                "wikidata": sorted({s.label for s in check.statements}) if check else [],
            }
        )
    return rows


def link_error_ids(checks: Sequence[LinkCheck]) -> list[str]:
    """The items involved in linking disagreements (to describe them in the report)."""
    ids = set()
    for check in checks:
        if not check.ambiguous and check.outcome in ("wrong", "unlinked"):
            ids.update(i for i in (check.linked_id, check.candidates[0]) if i)
    return sorted(ids)


def link_error_rows(
    checks: Sequence[LinkCheck], descriptions: Mapping[str, str], outcome: str
) -> list[dict[str, Any]]:
    """Names exactly one item carries, linked elsewhere (`wrong`) or not at all."""
    return [
        {
            "name": c.name,
            "type": c.type,
            "link_method": c.link_method,
            "linked_id": c.linked_id,
            "linked": descriptions.get(c.linked_id or "", ""),
            "label_item": c.candidates[0],
            "label_item_text": descriptions.get(c.candidates[0], ""),
        }
        for c in checks
        if not c.ambiguous and c.outcome == outcome
    ]


def build_report(
    name: str,
    data: ExtractionData,
    selection: str,
    link_checks: Sequence[LinkCheck],
    support_checks: Sequence[SupportCheck],
    sample: Sequence[RelationView],
    labels: Mapping[RelationKey, RelationLabelRecord],
    config: dict[str, Any],
    created_at: str,
    git: dict[str, Any],
    note: str = "",
    descriptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The whole extraction report as a JSON-ready dict."""
    support_by_key = {c.relation.key: c for c in support_checks}
    by_method = by_key(link_checks, lambda c: c.link_method)
    supported = [c for c in support_checks if c.supported]
    return {
        "name": name,
        "grouping_run": data.run_id,
        "source": data.source,
        "events_selected": selection,
        "created_at": created_at,
        "git": git,
        "note": note,
        "config": config,
        "overview": overview(data),
        "linking": {
            "all": count_links(link_checks).as_dict(),
            "by_method": {
                method: count_links(by_method[method]).as_dict()
                for method in ordered(by_method, LINK_METHODS)
            },
        },
        "support": support_summary(support_checks),
        "labels": count_labels(sample, labels).as_dict(),
        "by_type": type_rows(data, link_checks, support_checks, sample, labels),
        "sample": sample_rows(data, sample, labels, support_by_key),
        "link_errors": link_error_rows(link_checks, descriptions or {}, "wrong"),
        "missed_links": link_error_rows(link_checks, descriptions or {}, "unlinked"),
        "supported_examples": [
            {
                "subject": entity_label(data, c.relation.subject),
                "relation": c.relation.relation,
                "object": entity_label(data, c.relation.object),
                "wikidata": sorted({s.label for s in c.statements}),
            }
            for c in supported[:15]
        ],
    }


# --- Markdown -----------------------------------------------------------------------


def pct(value: float | None) -> str:
    """A share as a percentage with one decimal, or `n/a`."""
    return "n/a" if value is None else f"{100 * value:.1f}%"


def pct_pm(value: float | None, half_width: float | None) -> str:
    """A percentage with its 95% margin in percentage points."""
    if value is None:
        return "n/a"
    return pct(value) + (f" ± {100 * half_width:.1f} pp" if half_width else "")


def labelers_text(labels: dict[str, Any]) -> str:
    """Who labeled how many relations, as text."""
    who = ", ".join(f"{name}: {count}" for name, count in labels["labelers"].items())
    return who or "nobody yet"


def render_overview(report: dict[str, Any]) -> list[str]:
    """The report's overview section (Markdown lines)."""
    o = report["overview"]
    methods = ", ".join(f"{m} {n}" for m, n in o["by_link_method"].items())
    return [
        "## Overview",
        "",
        f"{o['events']} events, {o['entities']} distinct entities ({o['entity_mentions']} "
        f"mentions), {o['linked']} linked to Wikidata ({pct(o['linked_share'])}; {methods}). "
        f"{o['relations']} relations, {o['relations_both_linked']} between two linked "
        "entities.",
    ]


def render_headline(report: dict[str, Any]) -> list[str]:
    """The report's results table (Markdown lines)."""
    link, support, labels = report["linking"]["all"], report["support"], report["labels"]
    return [
        "## Results",
        "",
        "| Measure | Value | Based on |",
        "|---|---:|---|",
        f"| Linking accuracy (names exactly one Wikidata item carries) | "
        f"{pct_pm(link['accuracy'], link['accuracy_margin_95'])} | "
        f"{link['unique_correct'] + link['unique_wrong']} linked names |",
        f"| Linked to an item carrying the name (names several items carry) | "
        f"{pct(link['shared_rate'])} | {link['shared_candidate'] + link['shared_other']} "
        "linked names |",
        f"| Relation support in Wikidata (lower bound) | "
        f"{pct_pm(support['support_rate'], support['support_margin_95'])} | "
        f"{support['both_linked']} relations between linked entities |",
        f"| Labeled precision | {pct_pm(labels['precision'], labels['precision_margin_95'])} | "
        f"{labels['correct'] + labels['wrong']} of {labels['sampled']} sampled relations "
        f"({labels['unsure']} unsure, {labels['unlabeled']} unlabeled); labeled by "
        f"{labelers_text(labels)} |",
    ]


def render_types(report: dict[str, Any]) -> list[str]:
    """The report's table by entity type (Markdown lines)."""
    lines = [
        "## By entity type",
        "",
        "Relations count under the type of their subject.",
        "",
        "| Type | Entities | Linked | Linking accuracy | Relations (both linked) | "
        "Wikidata support | Sampled | Labeled precision |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["by_type"].items():
        link, support, labels = row["linking"], row["support"], row["labels"]
        lines.append(
            f"| {name} | {row['entities']} | {pct(row['linked_share'])} | "
            f"{pct(link['accuracy'])} ({link['unique_correct']}/"
            f"{link['unique_correct'] + link['unique_wrong']}) | "
            f"{support['relations']} ({support['both_linked']}) | "
            f"{pct(support['support_rate'])} ({support['supported']}/{support['both_linked']}) | "
            f"{labels['sampled']} | {pct(labels['precision'])} "
            f"({labels['correct']}/{labels['correct'] + labels['wrong']}) |"
        )
    return lines


def render_linking(report: dict[str, Any]) -> list[str]:
    """The report's linking details by link method (Markdown lines)."""
    lines = [
        "## Linking in detail",
        "",
        "| Link method | Names | No Wikidata label | One item: right / wrong / unlinked | "
        "Several items: one of them / other / unlinked | Accuracy |",
        "|---|---:|---:|---|---|---:|",
    ]
    for method, c in report["linking"]["by_method"].items():
        lines.append(
            f"| {method} | {c['names']} | {c['no_label']} | {c['unique_correct']} / "
            f"{c['unique_wrong']} / {c['unique_unlinked']} | {c['shared_candidate']} / "
            f"{c['shared_other']} / {c['shared_unlinked']} | {pct(c['accuracy'])} |"
        )
    for title, key in (
        ("Linked to another item than the one carrying the name", "link_errors"),
        ("Not linked, although exactly one item carries the name", "missed_links"),
    ):
        rows = report[key]
        lines += ["", f"{title} ({len(rows)}):", ""]
        for e in rows:
            linked = f"linked {e['linked_id']} ({e['linked']}), " if e["linked_id"] else ""
            lines.append(
                f"- *{e['name']}* ({e['type']}, {e['link_method']}): {linked}"
                f"label item {e['label_item']} ({e['label_item_text']})"
            )
        if not rows:
            lines.append("None.")
    return lines


def render_support(report: dict[str, Any]) -> list[str]:
    """The report's Wikidata support section (Markdown lines)."""
    support = report["support"]
    lines = ["## Wikidata support", "", "Properties that matched:", ""]
    lines += [f"- {prop}: {count}" for prop, count in support["properties"].items()]
    if not support["properties"]:
        lines.append("None.")
    lines += ["", "Examples:", ""]
    for e in report["supported_examples"]:
        lines.append(
            f"- {e['subject']} *{e['relation']}* {e['object']} "
            f"(Wikidata: {', '.join(e['wikidata'])})"
        )
    return lines


def render_sample(report: dict[str, Any]) -> list[str]:
    """The report's labeled sample (Markdown lines)."""
    lines = [
        "## Labeled sample",
        "",
        "| # | Relation | Source | Label | Labeler | Wikidata | Reason |",
        "|---:|---|---|---|---|---|---|",
    ]
    for number, row in enumerate(report["sample"], start=1):
        claim = f"{row['subject']} *{row['relation']}* {row['object']}"
        reason = row["reason"].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {number} | {claim} | {row['source_title']} | {row['label'] or '-'} | "
            f"{row['labeler'] or '-'} | {', '.join(row['wikidata']) or '-'} | {reason} |"
        )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """The extraction report as Markdown."""
    git = report["git"]
    lines = [
        f"# Extraction evaluation: {report['name']}",
        "",
        f"- Grouping run `{report['grouping_run']}`, {report['events_selected']}",
        f"- Extraction from: {report['source']}",
        f"- Created {report['created_at'][:19]} UTC, commit {git['commit']}"
        + (" (uncommitted changes)" if git["dirty"] else ""),
    ]
    if report["note"]:
        lines.append(f"- Note: {report['note']}")
    parts = (
        render_overview,
        render_headline,
        render_types,
        render_linking,
        render_support,
        render_sample,
    )
    for part in parts:
        lines += ["", *part(report)]
    lines += ["", "## Configuration", "", "```json", json.dumps(report["config"], indent=2)]
    lines += ["```"]
    return "\n".join(lines) + "\n"


def summary_line(report: dict[str, Any]) -> str:
    """The report's main numbers in one line."""
    link, support, labels = report["linking"]["all"], report["support"], report["labels"]
    return (
        f"{report['overview']['events']} events: linking accuracy {pct(link['accuracy'])} "
        f"({link['unique_correct'] + link['unique_wrong']} names), Wikidata support "
        f"{pct(support['support_rate'])} ({support['both_linked']} relations), labeled "
        f"precision {pct(labels['precision'])} ({labels['correct'] + labels['wrong']} labeled)"
    )


# --- Comparing two reports ----------------------------------------------------------

COMPARED = [
    ("Events", ("overview", "events")),
    ("Distinct entities", ("overview", "entities")),
    ("Linked share", ("overview", "linked_share")),
    ("Relations", ("overview", "relations")),
    ("Relations between linked entities", ("overview", "relations_both_linked")),
    ("Linking accuracy", ("linking", "all", "accuracy")),
    ("Names linked right (one item)", ("linking", "all", "unique_correct")),
    ("Wikidata support", ("support", "support_rate")),
    ("Supported relations", ("support", "supported")),
    ("Labeled precision", ("labels", "precision")),
    ("Labeled correct", ("labels", "correct")),
    ("Labeled wrong", ("labels", "wrong")),
]


def lookup(report: Mapping[str, Any], path: Sequence[str]) -> Any:
    """The value at `path` in a nested report, or None."""
    value: Any = report
    for part in path:
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


def show(value: Any) -> str:
    """A report value as text (shares as percentages)."""
    if isinstance(value, float):
        return pct(value)
    return "n/a" if value is None else str(value)


def compare_reports(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The compared measures of two reports, as rows of text."""
    return [(label, show(lookup(a, path)), show(lookup(b, path))) for label, path in COMPARED]
