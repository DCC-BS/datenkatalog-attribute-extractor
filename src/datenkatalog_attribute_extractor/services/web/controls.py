"""Turning observed controls into the listing the model reads.

What the page *shows* is measured in the browser (`docker/browser/observe.js`). What remains
here is what no single control can know: which controls are one field, and how the whole
inventory reads as text.

Grouping is the substantive part. The specification counts a set of related tick boxes as one
field named after its heading, and HTML says exactly which boxes are related — they share a
`name`. That is decided here rather than left to the model, so it stays testable.
"""

from dataclasses import (
    dataclass,
    field as dataclass_field,
    replace,
)

from datenkatalog_attribute_extractor.services.web.browser_client import ObservedControl

# Kinds where several controls sharing a name form one logical field.
GROUPED_KINDS = frozenset({"radio", "checkbox"})

MAX_OPTIONS_LISTED = 8


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
    indistinguishable from a form field by shape alone, and a rendered page is full of it. It
    is not decisive: plenty of modern forms are assembled from divs with no `<form>` at all,
    which is why this is reported rather than filtered on, and only where it discriminates
    (see `form_marker_is_informative`).
    """


def build_controls(observed: list[ObservedControl]) -> list[FormControl]:
    """Assemble the fields of a page from its observed controls, in reading order.

    Radio buttons and tick boxes sharing a `name` collapse into a single entry, because the
    specification counts such a group as one field named after its heading.

    Args:
        observed: The controls the browser reported, in reading order.

    Returns:
        One entry per field.
    """
    controls: list[FormControl] = []
    seen_groups: dict[tuple[str, ...], int] = {}

    for entry in observed:
        context = list(entry.context_path)

        if entry.kind in GROUPED_KINDS and entry.name:
            key = (entry.name, *context)
            if key in seen_groups:
                existing = controls[seen_groups[key]]
                if entry.label and entry.label not in existing.options and len(existing.options) < MAX_OPTIONS_LISTED:
                    existing.options.append(entry.label)
                continue

            seen_groups[key] = len(controls)
            # Whether this is a group or a lone tick box is not knowable yet — the deciding
            # fact is how many boxes share the name, and the rest have not been seen. Both
            # readings are kept and resolved in `_finalise_groups` once the count is in.
            controls.append(
                FormControl(
                    kind=f"{entry.kind}-gruppe",
                    label=context[-1] if context else entry.label,
                    context_path=context[:-1] if context else [],
                    name=entry.name,
                    options=[entry.label] if entry.label else [],
                    in_form=entry.in_form,
                )
            )
            continue

        controls.append(
            FormControl(
                kind=entry.kind,
                label=entry.label,
                context_path=context,
                name=entry.name,
                options=list(entry.options),
                in_form=entry.in_form,
            )
        )

    return _finalise_groups(controls)


def _finalise_groups(controls: list[FormControl]) -> list[FormControl]:
    """Turn one-member groups back into standalone controls.

    A set of related tick boxes is one field named after its heading, but a lone tick box — an
    "I accept the terms" — is a field named after its own caption. They are indistinguishable
    until the boxes are counted, so the distinction is made here.

    Args:
        controls: Controls in reading order, groups still provisional.

    Returns:
        The same controls with single-member groups relabelled.
    """
    finalised: list[FormControl] = []
    for control in controls:
        if control.kind.endswith("-gruppe") and len(control.options) == 1:
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
    one wrapping nothing but an upload widget — every real field is marked, and a model told
    that marked controls are usually page furniture answers by discarding the form.

    So the marker is emitted only where most controls do sit inside a form, which is the only
    case in which being outside one is unusual.

    Args:
        controls: Every control found on the page.

    Returns:
        Whether the listing should mark controls that no `<form>` encloses.
    """
    if not controls:
        return False
    return sum(1 for control in controls if control.in_form) * 2 >= len(controls)


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
