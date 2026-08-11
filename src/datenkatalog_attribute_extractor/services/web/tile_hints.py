"""The page's own spelling of the labels on one screen, handed to the model beside the picture.

A web form is read from pictures of it, because the picture is what a person fills in: it
carries the columns, the sections, the grouping of tick boxes under one question — everything a
control listing has to guess at and mostly guesses wrong. What the picture is *not* good at is
the exact wording of a label rendered in 11px type, and German form labels are long, hyphenated
and full of words a model will happily normalise into something more familiar.

So the labels the browser measured off the same render come along as a reference: not a second
inventory to reconcile, but a spelling aid, explicitly subordinate to the image. Only the
controls standing on the screens of *this* call are listed, which is what makes it a reference
for what is in front of the model rather than for the whole form.

Nothing is grouped here. Which tick boxes form one field is a question about what the form
asks, and the picture answers it — the markup only says which boxes share a `name`, which is
the same thing often enough to be trusted and different often enough to be wrong.
"""

from datenkatalog_attribute_extractor.services.web.browser_client import ObservedControl

HINT_HEADING = (
    "Wörtliche Beschriftungen der Bedienelemente auf den obigen Bildschirmen, wie die Seite "
    "sie ausgibt. Verwende sie ausschliesslich als Referenz für die genaue Schreibweise. Was "
    "ein Feld ist, welche Felder zusammengehören und unter welcher Überschrift sie stehen, "
    "liest du aus den Bildern:"
)

# A screen holds a screen's worth of controls; the cap only bounds a page that puts a thousand
# tick boxes in one viewport, where a truncated reference is still a reference.
MAX_HINT_LINES = 60


def render_hints(controls: list[ObservedControl]) -> str:
    """Render the labels of the controls on the screens of one call.

    Args:
        controls: The controls the browser saw standing on those screens, in reading order.
            Screens overlap, so the same control arrives more than once; repeats are dropped.

    Returns:
        The reference block, or an empty string when the screens offer nothing to spell — an
        unlabelled control says nothing a picture does not say better.
    """
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()

    for control in controls:
        label = control.label.strip()
        if not label:
            continue
        key = (control.kind, label.casefold())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- [{control.kind}] {label}")
        if len(lines) == MAX_HINT_LINES:
            break

    if not lines:
        return ""
    return "\n".join([HINT_HEADING, "", *lines])
