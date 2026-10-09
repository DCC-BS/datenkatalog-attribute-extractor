"""What a real browser makes of a real page, against the running browser service.

Excluded from CI: it needs `make docker-up-web`. Run locally with

    PYTHONPATH=src uv run --env-file .env python -m pytest tests/integration/test_observe_js.py -v

The unit tests work from canned observations, which pins down everything downstream of the
render but nothing about the render itself. This is where `docker/browser/observe.js` and the
step walk in `server.js` are actually exercised, and where the saved observations in
`tests/fixtures` are checked to still describe the pages they were captured from.

The pages are chosen to disagree with each other: one states every association in its markup,
one is a documentation site with a form buried in navigation, one states nothing at all.
"""

import json
import os
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BROWSER_URL = os.getenv("BROWSER_API_URL", "http://localhost:3100")

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") is None,
    reason="set RUN_BROWSER_TESTS=1 and start the browser service (make docker-up-web) to run these",
)


def walk(**payload) -> dict:
    """Ask the browser service to render something and walk it."""
    with httpx.Client(timeout=180) as client:
        response = client.post(f"{BROWSER_URL}/observe", json={"screenshots": False, "maxSteps": 1} | payload)
        response.raise_for_status()
        return response.json()


def observe(**payload) -> dict:
    """Render something and return the first step of it, which is all most of these look at."""
    return walk(**payload)["steps"][0]


def observe_fixture(name: str) -> dict:
    """Render a saved page and observe it."""
    return observe(html=(FIXTURES / f"{name}.html").read_text(encoding="utf-8"), waitMs=1500)


def saved(name: str) -> dict:
    """The observation committed for this page."""
    return json.loads((FIXTURES / f"{name}.observation.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["anmeldung_form", "w3schools_forms"])
def test_the_saved_observations_still_describe_these_pages(name: str) -> None:
    """The unit tests are only worth anything while their fixtures match what the browser does."""
    observed = observe_fixture(name)

    assert [control["label"] for control in observed["controls"]] == [
        control["label"] for control in saved(name)["controls"]
    ]


def test_markup_that_states_its_labels_is_believed() -> None:
    controls = observe_fixture("anmeldung_form")["controls"]

    by_name = {control["name"]: control for control in controls}
    assert by_name["schueler_familienname"]["label"] == "Familienname:"
    assert by_name["schueler_familienname"]["label_source"] == "markup"
    assert by_name["groesse"]["label"] == "Grösse (in cm)"


def test_a_page_that_states_nothing_is_read_from_its_layout() -> None:
    """The Vaadin form: no `name`, no `<label>`, no heading tag, no `<form>` around a field."""
    controls = observe_fixture("zetcom_vaadin_form")["controls"]

    labels = [control["label"] for control in controls]
    assert "Organisation*" in labels
    assert "PLZ*" in labels
    assert all(control["label_source"] in {"layout", ""} for control in controls)


def test_site_navigation_does_not_become_a_heading() -> None:
    """A menu item is styled exactly like a section title; what separates them is being a link."""
    controls = observe_fixture("w3schools_forms")["controls"]

    headings = {heading for control in controls for heading in control["context_path"]}
    assert "HTML Forms" in headings
    assert not any("Breadcrumb" in heading or "Tutorials" in heading for heading in headings)


def test_a_caption_split_by_a_link_is_read_as_one_sentence() -> None:
    html = """
    <body style="font: 16px sans-serif">
      <div><span>Ich habe die <a href="#">Wegleitung</a> gelesen *</span>
      <input type="checkbox" style="margin-left: 8px"></div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["Ich habe die Wegleitung gelesen *"]


def test_a_label_is_not_read_across_another_field() -> None:
    """Two columns of label and field: what disqualifies the far label is the near field."""
    html = """
    <body style="font: 16px sans-serif">
      <table><tr>
        <td>Vorname</td><td><input id="a"></td>
        <td>Nachname</td><td><input id="b"></td>
      </tr></table>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["Vorname", "Nachname"]


def test_a_caption_above_its_field_is_found() -> None:
    """The other common layout, and the one floating-label frameworks produce."""
    html = """
    <body style="font: 16px sans-serif">
      <div style="width: 300px">E-Mail-Adresse<br><input id="a" style="width: 280px"></div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["E-Mail-Adresse"]


def test_a_control_that_is_not_an_input_is_still_a_field() -> None:
    """A grid the applicant fills in row by row holds no `<input>` until a cell is opened."""
    html = """
    <body style="font: 16px sans-serif">
      <div>Beilagen*
        <div role="grid" style="width: 400px; height: 100px; display: inline-block">
          <div role="row"><div role="gridcell">Budget</div></div>
          <div role="row"><div role="gridcell">Offerte</div></div>
        </div>
      </div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert len(controls) == 1
    assert controls[0]["kind"] == "tabelle"
    assert controls[0]["label"] == "Beilagen*"
    assert controls[0]["options"] == ["Budget", "Offerte"]


def test_a_form_inside_an_iframe_is_found() -> None:
    """Embedded form providers put the whole form in a frame, where a page query finds nothing."""
    inner = "<label for='a'>Name des Vereins</label><input id='a'>"
    html = f'<body><h1>Anmeldung</h1><iframe srcdoc="{inner}" width="600" height="200"></iframe></body>'

    controls = observe(html=html, waitMs=300)["controls"]

    assert [control["label"] for control in controls] == ["Name des Vereins"]


def test_a_hidden_section_is_not_a_field() -> None:
    """A collapsed part of a form is not a field until it is opened."""
    html = '<body><div style="display:none"><label for="a">Versteckt</label><input id="a"></div></body>'

    assert observe(html=html, waitMs=100)["controls"] == []


def test_screenshots_are_produced_for_the_page() -> None:
    """The reading has nothing to work from without these."""
    html = "<body style='height: 3000px'><input id='a'></body>"

    tiles = observe(html=html, waitMs=100, screenshots=True)["tiles"]

    assert len(tiles) >= 2


def test_a_tile_reports_the_controls_standing_on_it() -> None:
    """What makes the label reference beside a screen a reference for that screen."""
    html = """
    <body style="font: 16px sans-serif">
      <div style="height: 1500px"><label for="a">Oben</label><input id="a"></div>
      <div><label for="b">Unten</label><input id="b"></div>
    </body>
    """
    step = observe(html=html, waitMs=200, screenshots=True)
    labels = [[step["controls"][index]["label"] for index in tile["control_indices"]] for tile in step["tiles"]]

    assert labels[0] == ["Oben"]
    assert "Unten" in labels[-1]


WIZARD = """
<body style="font: 16px sans-serif">
  <div id="one"><label for="a">Meldung betrifft</label><input id="a" required></div>
  <div id="two" style="display: none"><label for="b">Vorname</label><input id="b"></div>
  <button id="next" onclick="
    if (!document.getElementById('a').value) { document.title = 'blocked'; return; }
    document.getElementById('one').style.display = 'none';
    document.getElementById('two').style.display = 'block';
    this.remove();
  ">Weiter</button>
</body>
"""


def test_a_wizard_is_walked_through_its_steps() -> None:
    """The required field is answered, the page's own next button pressed, the step read."""
    result = walk(html=WIZARD, waitMs=200, maxSteps=4, stepWaitMs=300)

    labels = [[control["label"] for control in step["controls"]] for step in result["steps"]]
    assert labels == [["Meldung betrifft"], ["Vorname"]]
    assert result["stopped_because"] == "no_next"


def test_a_wizard_is_not_walked_when_only_one_step_is_asked_for() -> None:
    result = walk(html=WIZARD, waitMs=200, maxSteps=1)

    assert len(result["steps"]) == 1


SUBMIT_FORM = """
<body style="font: 16px sans-serif">
  <label for="a">Vorname</label><input id="a" required>
  <button onclick="document.body.innerHTML = '<p>abgeschickt</p>'">Absenden</button>
</body>
"""


def test_a_submit_button_is_never_pressed() -> None:
    """A form service takes a filled-in form at its word; reading one must not send it."""
    result = walk(html=SUBMIT_FORM, waitMs=200, maxSteps=4, stepWaitMs=300)

    assert result["stopped_because"] == "no_next"
    assert [control["label"] for control in result["steps"][0]["controls"]] == ["Vorname"]


def test_a_form_that_will_not_advance_says_so_rather_than_looping() -> None:
    """`autofill: false` leaves the required field empty, so the wizard refuses to move on."""
    result = walk(html=WIZARD, waitMs=200, maxSteps=4, stepWaitMs=300, autofill=False)

    assert len(result["steps"]) == 1
    assert result["stopped_because"] == "unchanged"


# Marks nothing required until it has refused once, which is what jaxforms does: the first
# Weiter is answered with `aria-invalid` on the field it wanted all along.
LATE_VALIDATION = """
<body style="font: 16px sans-serif">
  <div id="one">
    <label for="a">Ort</label><input id="a">
    <label for="b">Bemerkung</label><input id="b">
  </div>
  <div id="two" style="display: none"><label for="c">Vorname</label><input id="c"></div>
  <button id="next" onclick="
    const ort = document.getElementById('a');
    if (!ort.value) { ort.setAttribute('aria-invalid', 'true'); return; }
    document.getElementById('one').style.display = 'none';
    document.getElementById('two').style.display = 'block';
    this.remove();
  ">Weiter</button>
</body>
"""


def test_a_step_that_says_what_it_wanted_only_after_refusing_is_answered() -> None:
    """A step that marks nothing required is got past all the same.

    It used to take two attempts here — fill what is marked, be refused, fill what the refusal
    marked. The walk now answers every empty control on the first pass, so the demand never has
    to be announced, and this asserts the outcome rather than the route to it. That a refusal
    *is* acted on is covered by the wizard fixture, whose consent box is ticked only after the
    step has said no: see `tests/integration/test_never_submit.py`.
    """
    result = walk(html=LATE_VALIDATION, waitMs=200, maxSteps=4, stepWaitMs=300)

    labels = [[control["label"] for control in step["controls"]] for step in result["steps"]]
    assert labels == [["Ort", "Bemerkung"], ["Vorname"]]


# Drops anything that is not digits, the way the KESB postcode field does.
MASKED = """
<body style="font: 16px sans-serif">
  <div id="one">
    <label for="a">PLZ</label>
    <input id="a" required oninput="this.value = this.value.replace(/\\D/g, '')">
  </div>
  <div id="two" style="display: none"><label for="b">Ort</label><input id="b"></div>
  <button onclick="
    if (!document.getElementById('a').value) return;
    document.getElementById('one').style.display = 'none';
    document.getElementById('two').style.display = 'block';
    this.remove();
  ">Weiter</button>
</body>
"""


def test_a_field_that_drops_what_does_not_fit_its_mask_is_filled_anyway() -> None:
    """A word is tried first and vanishes; the digits behind it in the candidate list stick."""
    result = walk(html=MASKED, waitMs=200, maxSteps=4, stepWaitMs=300)

    assert [control["label"] for control in result["steps"][-1]["controls"]] == ["Ort"]


# Clears itself unless the value came from its own suggestion list, like a jQuery UI
# autocomplete. `fill` is deliberately defeated: only real keystrokes open the list.
AUTOCOMPLETE = """
<body style="font: 16px sans-serif">
  <div id="one">
    <label for="a">Gemeinde</label>
    <input id="a" required aria-autocomplete="list" autocomplete="off">
    <ul id="menu" style="display: none; list-style: none"><li role="option">Basel</li></ul>
  </div>
  <div id="two" style="display: none"><label for="b">Strasse</label><input id="b"></div>
  <script>
    const field = document.getElementById("a");
    const menu = document.getElementById("menu");
    field.addEventListener("keyup", () => { menu.style.display = field.value ? "block" : "none"; });
    field.addEventListener("blur", () => { if (!field.dataset.picked) field.value = ""; });
    menu.querySelector("li").addEventListener("mousedown", () => {
      field.dataset.picked = "1";
      field.value = "Basel";
      menu.style.display = "none";
    });
  </script>
  <button onclick="
    if (!document.getElementById('a').value) return;
    document.getElementById('one').style.display = 'none';
    document.getElementById('two').style.display = 'block';
    this.remove();
  ">Weiter</button>
</body>
"""


def test_a_field_that_only_keeps_what_it_suggested_is_filled_from_its_suggestions() -> None:
    result = walk(html=AUTOCOMPLETE, waitMs=200, maxSteps=4, stepWaitMs=300)

    assert [control["label"] for control in result["steps"][-1]["controls"]] == ["Strasse"]


# The last button of a wizard. Pressing it is the one thing the walk must never do, and a
# `<form>` makes the second way of doing it available: Enter in a text field submits.
SUBMIT_WIZARD = """
<body style="font: 16px sans-serif">
  <form onsubmit="document.body.innerHTML = '<p>abgeschickt</p>'; return false;">
    <label for="a">Gemeinde</label>
    <input id="a" required aria-autocomplete="list" autocomplete="off">
    <button type="submit">Absenden</button>
  </form>
</body>
"""


# A next button that is present and greyed out, next to a field nothing will satisfy.
BLOCKED = """
<body style="font: 16px sans-serif">
  <label for="a">Referenznummer</label>
  <input id="a" required oninput="this.value = ''">
  <button id="next" disabled>Weiter</button>
</body>
"""


def test_a_greyed_out_next_button_is_not_the_end_of_the_form() -> None:
    """Reporting it as "no further step" would describe a wizard as a one-page form."""
    result = walk(html=BLOCKED, waitMs=200, maxSteps=4, stepWaitMs=300)

    assert result["stopped_because"] == "blocked"
    assert result["steps"][0]["blocked_by"] == ["Referenznummer"]


# A time field wearing its mask. `element.value` is never empty, and the first keystroke after
# a click goes into moving the caret rather than into the field.
MASKED_TIME = """
<body style="font: 16px sans-serif">
  <div id="one">
  <label for="a">Beginn</label>
  <input id="a" value="__:__" aria-invalid="true" oninput="
    const digits = this.value.replace(/\\D/g, '').slice(0, 4).padEnd(4, '_');
    this.value = digits.slice(0, 2) + ':' + digits.slice(2);
    this.setAttribute('aria-invalid', /_/.test(this.value) ? 'true' : 'false');
  ">
  </div>
  <div id="two" style="display: none"><label for="b">Ende</label><input id="b"></div>
  <button onclick="
    if (document.getElementById('a').getAttribute('aria-invalid') === 'true') return;
    document.getElementById('one').style.display = 'none';
    document.getElementById('two').style.display = 'block';
    this.remove();
  ">Weiter</button>
</body>
"""


def test_a_field_wearing_its_mask_is_recognised_as_empty_and_filled() -> None:
    """`__:__` is not a value, and a field holding one is skipped unless that is noticed."""
    result = walk(html=MASKED_TIME, waitMs=200, maxSteps=4, stepWaitMs=300)

    assert [control["label"] for control in result["steps"][-1]["controls"]] == ["Ende"]


def test_filling_a_form_never_submits_it() -> None:
    """Typing into the last step must not commit the form by the back door."""
    result = walk(html=SUBMIT_WIZARD, waitMs=200, maxSteps=3, stepWaitMs=300)

    assert result["stopped_because"] == "no_next"
    assert [control["label"] for control in result["steps"][0]["controls"]] == ["Gemeinde"]
