"""The one promise this project cannot break: it never hands a filled-in form to an authority.

Excluded from CI: it needs a real browser and the form fixture beside it. Run locally with

    make docker-up-web
    RUN_BROWSER_TESTS=1 PYTHONPATH=src uv run --env-file .env python -m pytest \
        tests/integration/test_never_submit.py -v

Every assertion here is made against the *fixture's* record of what reached it, never against
the browser service's account of what it pressed. `docker/wizard` records every hit on its
`/submit` endpoint; a walk that reaches the last step of it must leave that record empty. The
distinction matters because the two can disagree: the walk classifies a button by its wording,
and the whole point of the network guard is to be right when that classification is wrong.

The fixture reproduces, in one form, every way the cantonal forms have refused to be walked —
see `docker/wizard/server.js`.
"""

import os

import httpx
import pytest

BROWSER_URL = os.getenv("BROWSER_API_URL", "http://localhost:3100")

# Two addresses for one service: the test reaches the fixture through the published port, the
# browser reaches it by service name on the compose network.
WIZARD_URL = os.getenv("WIZARD_URL", "http://localhost:3200")
WIZARD_INTERNAL_URL = os.getenv("WIZARD_INTERNAL_URL", "http://wizard:3200")

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") is None,
    reason="set RUN_BROWSER_TESTS=1 and start the browser and fixture (make docker-up-web) to run these",
)


def reset_wizard() -> None:
    """Forget every submission the fixture has recorded."""
    with httpx.Client(timeout=10) as client:
        client.post(f"{WIZARD_URL}/__reset").raise_for_status()


def submissions() -> list[dict]:
    """What the fixture says was actually filed with it."""
    with httpx.Client(timeout=10) as client:
        response = client.get(f"{WIZARD_URL}/__audit")
        response.raise_for_status()
        return response.json()["submissions"]


def walk(**payload) -> dict:
    """Walk the fixture wizard with the browser service."""
    body = {
        "url": WIZARD_INTERNAL_URL,
        "screenshots": False,
        "maxSteps": 6,
        "waitMs": 500,
        "stepWaitMs": 1500,
    } | payload
    with httpx.Client(timeout=300) as client:
        response = client.post(f"{BROWSER_URL}/observe", json=body)
        response.raise_for_status()
        return response.json()


@pytest.fixture(autouse=True)
def _clean_fixture() -> None:
    """Each test starts with the fixture having received nothing."""
    reset_wizard()


def test_a_walk_that_reaches_the_last_step_files_nothing() -> None:
    """The whole promise, stated once: walk the wizard to its end, submit nothing.

    `navigation` is the level a walk of a server-validated form needs — each step is a form POST
    the page cannot get past otherwise — so this is the level at which the promise is hardest to
    keep and the only one worth asserting it at.
    """
    result = walk(blockWrites="navigation")

    assert submissions() == [], "the walk filed the application with the authority"
    # Four steps: the two that validate on the server, the one whose next button is disabled
    # until answered, and the summary the walk must stop on.
    assert len(result["steps"]) >= 4, f"the walk stopped early: {result['stopped_because']}"
    assert result["stopped_because"] == "no_next"

    # Each step recorded once. A step that refuses re-renders with its captions rewritten, and
    # counting that as a new step is what used to hand the model pictures of a form covered in
    # "Feld darf nicht leer sein".
    assert [step["title"] for step in result["steps"]] == [
        "Art der Veranstaltung",
        "Kontaktangaben",
        "Angaben zur Veranstaltung",
        "Zusammenfassung",
    ]
    assert not [
        control["label"] for step in result["steps"] for control in step["controls"] if "nicht leer" in control["label"]
    ], "a step was recorded in its refused state"

    # The consent box is the one control never answered on sight: step 3 is pressed, refused,
    # and only then is the box the page marks required ticked.
    assert result["steps"][2]["attempts"] == 2


def test_the_summary_step_is_where_it_stops() -> None:
    """Neither button on the last step is pressed, including the one that says *Weiter*.

    `Weiter zur zahlungspflichtigen Bestellung` is the trap: it opens with the word the walk
    looks for and ends with the thing it must never do.
    """
    result = walk(blockWrites="navigation")

    assert submissions() == []
    assert [step["advanced_by"] for step in result["steps"]][-1] == "", "something on the summary was pressed"


def test_a_background_submit_is_refused_although_nothing_was_clicked() -> None:
    """A script that posts to a submit endpoint on its own is stopped by the network, not by us.

    Step 3 of the fixture fires this without any button being touched, which is precisely the
    case a denylist over button wording cannot see. It is also the check that the guard's one
    unconditional rule works: an XHR write would otherwise be allowed at this level.
    """
    result = walk(blockWrites="navigation")

    assert submissions() == []
    refused = [entry for entry in result["network_audit"]["entries"] if entry["reason"] == "submit_path"]
    assert refused, "the background POST to /submit was not seen by the guard"
    assert all(entry["allowed"] is False for entry in refused)


def test_a_websocket_is_refused_before_it_is_connected() -> None:
    """Request interception never sees a websocket, so something else has to refuse it.

    Step 1 of the fixture opens one and sends on it. A form framework that pushes over a socket
    — Vaadin, which is what one of the cantonal forms is built on — could carry everything typed
    into the form out that way with the guard reporting a perfectly quiet run.
    """
    result = walk(blockWrites="navigation")

    refused = [entry for entry in result["network_audit"]["entries"] if entry["reason"] == "websocket_blocked"]
    assert refused, "the websocket was opened without the guard seeing it"
    assert all(entry["allowed"] is False for entry in refused)


def test_the_strictest_level_lets_nothing_out_and_says_so() -> None:
    """At `all`, a server-validated step cannot be passed — and that is the intended answer.

    This is the level an evidence run against an unfamiliar form service uses: the walk still
    fills in and presses, so what the form *would* have sent is recorded, and none of it leaves.
    """
    result = walk(blockWrites="all")

    assert submissions() == []
    audit = result["network_audit"]
    assert audit["level"] == "all"
    assert audit["blocked"] > 0
    assert {entry["reason"] for entry in audit["entries"] if not entry["allowed"]} & {"write_blocked", "submit_path"}
    # The first step's POST never leaves, so the walk cannot get past it.
    assert len(result["steps"]) == 1

    # One page load, and nothing else that could carry an answer anywhere. A form that advanced
    # by GET would otherwise put what was typed into a query string and send it.
    allowed = [entry for entry in audit["entries"] if entry["allowed"]]
    assert allowed == [], f"something was allowed to leave at the strictest level: {allowed}"
