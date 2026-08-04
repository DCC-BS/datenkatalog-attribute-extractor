"""Reduces a scraped page to the form controls it contains.

A real form page is mostly not the form: the 429 KB of HTML behind a seven-field
questionnaire is navigation, styling, tracking and cookie banners. Sending that to the model
wastes context that the served `max_model_len` does not have to spare, and buries the fields
in noise.

So the DOM is reduced to one line per control, carrying only what naming needs: a candidate
label, the heading and legend chain around it, and enough about the control to tell a group
from a single field. This is where the web path earns its keep over reading a picture of the
page — `label for`, `fieldset`/`legend` and heading nesting state the structure outright,
where a screenshot would leave the model inferring it from pixels.

Grouping matters as much as labelling. The specification counts a set of related tick boxes
as *one* field named after its heading, and HTML says exactly which boxes are related: they
share a `name`. That is decided here rather than left to the model.
"""

from dataclasses import (
    dataclass,
    field as dataclass_field,
    replace,
)

from selectolax.parser import HTMLParser, Node

# Dropped wholesale: none of it can contain a form field, and some of it (script, style) is
# most of the page by volume.
NOISE_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "iframe", "template", "head", "link", "meta"})

# Controls a person cannot type into, or that submit rather than collect.
SKIPPED_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})

# Types where several controls sharing a name form one logical field.
GROUPED_INPUT_TYPES = frozenset({"radio", "checkbox"})

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

CONTROL_TAGS = frozenset({"input", "select", "textarea"})

MAX_OPTIONS_LISTED = 8
MAX_LABEL_LENGTH = 200


@dataclass(frozen=True, slots=True, kw_only=True)
class FormControl:
    """One control, or one group of related controls, as offered to the model."""

    kind: str
    label: str
    context_path: list[str]
    name: str = ""
    options: list[str] = dataclass_field(default_factory=list)
    in_form: bool = True
    """Whether a `<form>` encloses this control.

    Page furniture — the site search box, a filter input, a dark-mode toggle — is
    indistinguishable from a form field by shape alone, and a scraped page is full of it. Most
    such controls sit outside any `<form>`, so this is the single most useful signal the model
    has for telling the questionnaire from the website around it. It is not decisive: plenty
    of modern forms are assembled from divs with no `<form>` at all, which is why this is
    reported rather than filtered on.
    """


def _clean(text: str | None) -> str:
    """Collapse whitespace in text taken from the page."""
    if not text:
        return ""
    return " ".join(text.split())[:MAX_LABEL_LENGTH]


def _strip_noise(tree: HTMLParser) -> None:
    """Remove nodes that cannot contain a form field."""
    for tag in NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()


def _label_texts_by_id(tree: HTMLParser) -> dict[str, str]:
    """Map each `id` to the text of the `<label for=...>` that points at it."""
    mapping: dict[str, str] = {}
    for label in tree.css("label[for]"):
        target = label.attributes.get("for")
        text = _clean(label.text())
        if target and text and target not in mapping:
            mapping[target] = text
    return mapping


def _text_by_id(tree: HTMLParser, element_id: str) -> str:
    """Return the text of the element with this id, for `aria-labelledby`."""
    node = tree.css_first(f"[id='{element_id}']")
    return _clean(node.text()) if node is not None else ""


def _enclosing_label(node: Node) -> str:
    """Return the text of a `<label>` that wraps this control, if any."""
    parent = node.parent
    while parent is not None:
        if parent.tag == "label":
            return _clean(parent.text())
        parent = parent.parent
    return ""


def _is_inside_form(node: Node) -> bool:
    """Report whether a `<form>` encloses this control."""
    parent = node.parent
    while parent is not None:
        if parent.tag == "form":
            return True
        parent = parent.parent
    return False


def _legend_chain(node: Node) -> list[str]:
    """Return the `<legend>` of every enclosing `<fieldset>`, outermost first."""
    chain: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.tag == "fieldset":
            legend = parent.css_first("legend")
            text = _clean(legend.text()) if legend is not None else ""
            if text:
                chain.append(text)
        parent = parent.parent
    return list(reversed(chain))


def _resolve_label(node: Node, tree: HTMLParser, labels_by_id: dict[str, str]) -> str:
    """Find the best available label for a control.

    Tried in descending order of reliability: an explicit `label for`, a label wrapping the
    control, the ARIA attributes, then the hints that are only sometimes labels — the title,
    the placeholder, and finally the control's own name.
    """
    attributes = node.attributes

    element_id = attributes.get("id")
    if element_id and element_id in labels_by_id:
        return labels_by_id[element_id]

    wrapped = _enclosing_label(node)
    if wrapped:
        return wrapped

    aria_label = _clean(attributes.get("aria-label"))
    if aria_label:
        return aria_label

    labelled_by = attributes.get("aria-labelledby")
    if labelled_by:
        referenced = " ".join(filter(None, (_text_by_id(tree, ref) for ref in labelled_by.split())))
        if referenced:
            return _clean(referenced)

    for attribute in ("title", "placeholder"):
        value = _clean(attributes.get(attribute))
        if value:
            return value

    return _clean(attributes.get("name")) or ""


def _control_kind(node: Node) -> str:
    """Describe what sort of control this is, for the listing."""
    if node.tag == "select":
        return "auswahlliste"
    if node.tag == "textarea":
        return "textfeld mehrzeilig"
    return (node.attributes.get("type") or "text").lower()


def _select_options(node: Node) -> list[str]:
    """Return the visible options of a `<select>`, capped."""
    options = [_clean(option.text()) for option in node.css("option")]
    return [option for option in options if option][:MAX_OPTIONS_LISTED]


def _is_skipped(node: Node) -> bool:
    """Report whether this control collects nothing a reviewer would name."""
    if node.tag != "input":
        return False
    return (node.attributes.get("type") or "text").lower() in SKIPPED_INPUT_TYPES


def extract_controls(html: str) -> list[FormControl]:
    """Reduce a page to the form controls it offers, in document order.

    Radio buttons and checkboxes sharing a `name` collapse into a single entry, because the
    specification counts such a group as one field named after its heading.

    Args:
        html: The raw HTML of the scraped page.

    Returns:
        One entry per field, in the order they appear on the page.
    """
    tree = HTMLParser(html)
    _strip_noise(tree)
    labels_by_id = _label_texts_by_id(tree)

    controls: list[FormControl] = []
    heading_stack: list[tuple[int, str]] = []
    seen_groups: dict[tuple[str, ...], int] = {}

    for node in tree.root.traverse(include_text=False) if tree.root is not None else []:
        tag = node.tag

        if tag in HEADING_TAGS:
            level = HEADING_TAGS[tag]
            text = _clean(node.text())
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            if text:
                heading_stack.append((level, text))
            continue

        if tag not in CONTROL_TAGS or _is_skipped(node):
            continue

        context = [text for _, text in heading_stack]
        context.extend(legend for legend in _legend_chain(node) if legend not in context)

        kind = _control_kind(node)
        name = _clean(node.attributes.get("name"))
        label = _resolve_label(node, tree, labels_by_id)
        in_form = _is_inside_form(node)

        if kind in GROUPED_INPUT_TYPES and name:
            key = (name, *context)
            if key in seen_groups:
                index = seen_groups[key]
                existing = controls[index]
                option = label or _clean(node.attributes.get("value"))
                if option and option not in existing.options and len(existing.options) < MAX_OPTIONS_LISTED:
                    existing.options.append(option)
                continue
            seen_groups[key] = len(controls)
            # Whether this is a group or a lone checkbox is not knowable yet — the deciding
            # fact is how many boxes share the name, and the rest have not been seen. Both
            # readings are kept and resolved in _finalise_groups once the count is in.
            controls.append(
                FormControl(
                    kind=f"{kind}-gruppe",
                    label=context[-1] if context else label,
                    context_path=context[:-1] if context else [],
                    name=name,
                    options=[label] if label else [],
                    in_form=in_form,
                )
            )
            continue

        controls.append(
            FormControl(
                kind=kind,
                label=label,
                context_path=context,
                name=name,
                options=_select_options(node) if tag == "select" else [],
                in_form=in_form,
            )
        )

    return _finalise_groups(controls)


def _finalise_groups(controls: list[FormControl]) -> list[FormControl]:
    """Turn one-member groups back into standalone controls.

    A set of related tick boxes is one field named after its heading, but a lone checkbox —
    an "I accept the terms" — is a field named after its own caption. They are the same
    markup until the boxes are counted, so the distinction is made here.

    Args:
        controls: Controls in document order, groups still provisional.

    Returns:
        The same controls with single-member groups relabelled.
    """
    finalised: list[FormControl] = []
    for control in controls:
        is_group = control.kind.endswith("-gruppe")
        if is_group and len(control.options) == 1:
            finalised.append(
                replace(
                    control,
                    kind=control.kind.removesuffix("-gruppe"),
                    label=control.options[0],
                    context_path=[*control.context_path, control.label] if control.label else control.context_path,
                    options=[],
                )
            )
            continue
        finalised.append(control)
    return finalised


def render_listing(controls: list[FormControl], *, title: str = "") -> str:
    """Render controls as the compact text the model is shown.

    Headings become markdown headings so the nesting is visible without explanation, and the
    model can lift `context_path` straight out of them.

    Args:
        controls: The controls found on the page.
        title: The page title, used as a first line of orientation.

    Returns:
        The listing, ready to send.
    """
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")

    current: list[str] = []
    for control in controls:
        if control.context_path != current:
            current = control.context_path
            for depth, heading in enumerate(current, start=2):
                lines.append(f"{'#' * min(depth, 6)} {heading}")

        parts = [f"- [{control.kind}]"]
        parts.append(f'"{control.label}"' if control.label else "(ohne Beschriftung)")
        if control.name:
            parts.append(f"(name={control.name})")
        if control.options:
            parts.append(f"— Optionen: {' | '.join(control.options)}")
        if not control.in_form:
            parts.append("[ausserhalb eines <form>]")
        lines.append(" ".join(parts))

    return "\n".join(lines)
