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

Not every page states any of that. A widget toolkit that generates its DOM — Vaadin, and most
component frameworks — emits bare `<input>` elements with no `name`, no `<label for>`, no
`<form>` and no heading tags: the caption is a neighbouring table cell, the section title is a
bold `<div>`. Read under the rules above, such a page reduces to a listing of unlabelled
controls, which is worth no more to the model than nothing at all. So where the markup states
no association, the neighbouring text and the implied headings are used — always as a
fallback, never in front of an association the markup does state.
"""

import re
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

CONTROL_SELECTOR = "input, select, textarea"

MAX_OPTIONS_LISTED = 8
MAX_LABEL_LENGTH = 200

# Icon fonts (Font Awesome and friends) draw their glyphs from the Unicode private use area.
# They read as text and carry none, and a control whose neighbour is an icon would otherwise
# be labelled with one.
PRIVATE_USE_FIRST = "\ue000"
PRIVATE_USE_LAST = "\uf8ff"

# How far up the tree the neighbouring text of a control is searched. Measured against a
# Vaadin form, whose captions sit twelve levels above the control they describe; the layout
# wrappers a widget toolkit generates are what makes this deep rather than any real nesting.
MAX_LABEL_SEARCH_DEPTH = 12

# Neighbouring text longer than this is prose — an introduction, a legal note — not a caption.
MAX_PROXIMITY_LABEL_LENGTH = 120

# Level given to a heading the markup only implies. Deeper than every real heading, so an
# `h1`-`h6` that follows one always replaces it.
IMPLIED_HEADING_LEVEL = 7

# A section title a generated DOM writes as emphasis rather than as a heading.
IMPLIED_HEADING_TAGS = frozenset({"b", "strong"})

MAX_IMPLIED_HEADING_LENGTH = 80

# The other way a generated DOM writes a section title: an ordinary element made bold inline.
BOLD_STYLE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.IGNORECASE)


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
    """Collapse whitespace in text taken from the page, dropping icon glyphs."""
    if not text:
        return ""
    visible = "".join(character for character in text if not PRIVATE_USE_FIRST <= character <= PRIVATE_USE_LAST)
    return " ".join(visible.split())[:MAX_LABEL_LENGTH]


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


def _caption_candidate(node: Node) -> str:
    """Return this node's text if it could be a caption for a nearby control.

    A node holding a control of its own is not a caption — it is another field, and its text
    belongs to that field. Prose is rejected on length: a page's introduction sits next to the
    first control as readily as its caption does.
    """
    if node.tag != "-text" and node.css_first(CONTROL_SELECTOR) is not None:
        return ""

    text = _clean(node.text())
    return text if len(text) <= MAX_PROXIMITY_LABEL_LENGTH else ""


def _neighbouring_text(node: Node) -> str:
    """Return the text nearest to a control that no association points at.

    The search widens one level at a time — siblings first, then the parent's siblings, and so
    on — so the closest text wins, which is what a person reading the page sees as the
    caption. Text before the control is preferred at every level, because that is where a
    caption sits; text after it is the checkbox case, where the caption follows the box.

    Args:
        node: The control being labelled.

    Returns:
        The nearest plausible caption, or an empty string.
    """
    current = node
    for _ in range(MAX_LABEL_SEARCH_DEPTH):
        if current.parent is None:
            break

        for step in ("prev", "next"):
            sibling: Node | None = getattr(current, step)
            while sibling is not None:
                candidate = _caption_candidate(sibling)
                if candidate:
                    return candidate
                sibling = getattr(sibling, step)

        current = current.parent

    return ""


def _resolve_label(node: Node, tree: HTMLParser, labels_by_id: dict[str, str], context: list[str]) -> str:
    """Find the best available label for a control.

    Tried in descending order of reliability: an explicit `label for`, a label wrapping the
    control, the ARIA attributes, then the hints that are only sometimes labels — the title,
    the placeholder, the neighbouring text, and finally the control's own name.

    The neighbouring text ranks above `name` because it is what a person reads as the caption,
    while `name` is an identifier that may be `ProFormApplicantFld` or nothing at all. It is
    ignored when it merely repeats an enclosing heading: that text is already in the context
    path, and repeating it as a label would invent a field called after its own section.

    Args:
        node: The control being labelled.
        tree: The page, for `aria-labelledby` lookups.
        labels_by_id: Text of every `<label for=...>` on the page.
        context: The heading chain the control sits under.
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

    neighbouring = _neighbouring_text(node)
    if neighbouring and neighbouring not in context:
        return neighbouring

    return _clean(attributes.get("name")) or ""


def _heading_level(node: Node) -> int:
    """Return the heading level of this node, or 0 if it is not a heading.

    Beyond `h1`-`h6` three implied forms count. `role="heading"` says outright that the element
    is one, and its `aria-level` gives the depth. Bold text that is the entire content of its
    block — `<b>Gesuchsteller</b>`, or a `<td style="font-weight: bold">Angaben zum Gesuch</td>`
    — is how a generated DOM writes a section title; the same page uses both. Requiring the
    text to be short and to be all the block holds is what stops a bold word inside a sentence
    from becoming a heading and swallowing every field that follows it.
    """
    if node.tag in HEADING_TAGS:
        return HEADING_TAGS[node.tag]

    if node.attributes.get("role") == "heading":
        level = node.attributes.get("aria-level") or ""
        return int(level) if level.isdigit() and 1 <= int(level) <= len(HEADING_TAGS) else IMPLIED_HEADING_LEVEL

    if node.tag not in IMPLIED_HEADING_TAGS and not BOLD_STYLE.search(node.attributes.get("style") or ""):
        return 0

    if node.css_first(CONTROL_SELECTOR) is not None:
        return 0

    text = _clean(node.text())
    if not text or len(text) > MAX_IMPLIED_HEADING_LENGTH:
        return 0

    parent = node.parent
    if node.tag in IMPLIED_HEADING_TAGS and (
        parent is None or parent.tag in HEADING_TAGS or _clean(parent.text()) != text
    ):
        return 0

    return IMPLIED_HEADING_LEVEL


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

        level = _heading_level(node)
        if level:
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
        label = _resolve_label(node, tree, labels_by_id, context)
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


def form_marker_is_informative(controls: list[FormControl]) -> bool:
    """Report whether `[ausserhalb eines <form>]` tells the model anything on this page.

    The marker separates the questionnaire from the website around it only where the page
    draws that line itself. On a page whose form is built from divs — no `<form>` at all, or
    one wrapping nothing but a file-upload widget — every real field is marked, and a model
    told that marked controls are usually page furniture answers by discarding the form. That
    is exactly what a Vaadin page produced: one `<form>` around the upload widget, twenty
    fields outside it, no fields reported.

    So the marker is emitted only where most controls do sit inside a form, which is the only
    case in which being outside one is unusual.

    Args:
        controls: Every control found on the page.

    Returns:
        Whether the listing should mark controls that no `<form>` encloses.
    """
    if not controls:
        return False
    inside = sum(1 for control in controls if control.in_form)
    return inside * 2 >= len(controls)


def render_listing(controls: list[FormControl], *, title: str = "", mark_outside_form: bool | None = None) -> str:
    """Render controls as the compact text the model is shown.

    Headings become markdown headings so the nesting is visible without explanation, and the
    model can lift `context_path` straight out of them.

    Args:
        controls: The controls found on the page.
        title: The page title, used as a first line of orientation.
        mark_outside_form: Whether to mark controls no `<form>` encloses. Defaults to deciding
            from the controls given; a caller rendering one chunk of a page must pass the
            decision made for the whole page, because a chunk is not a big enough sample.

    Returns:
        The listing, ready to send.
    """
    mark_outside = form_marker_is_informative(controls) if mark_outside_form is None else mark_outside_form
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
        if mark_outside and not control.in_form:
            parts.append("[ausserhalb eines <form>]")
        lines.append(" ".join(parts))

    return "\n".join(lines)
