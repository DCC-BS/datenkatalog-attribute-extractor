/**
 * A wizard that misbehaves in every way a real cantonal form has misbehaved.
 *
 * It exists so that the one thing this project must never do — hand a filled-in form to an
 * authority — can be *tested* rather than reasoned about. A saved HTML page cannot do that: the
 * failures that matter are server-side validation, a step that only says what it wanted after
 * refusing, and a final button that files the case. Those need a server, so this is one.
 *
 * Four steps, each carrying a failure paid for on a live form:
 *
 * 1. *Art der Veranstaltung* — a radio group marked as required nowhere at all, and a next
 *    button that stays disabled until it is answered (the KESB form's first step).
 * 2. *Kontaktangaben* — nothing marked required, and the server saying what it wanted only
 *    after refusing; a postcode that silently drops what does not fit; a time field wearing
 *    `__:__` so it never reads as empty; a town that only accepts a value picked from its own
 *    suggestions and wipes anything put in programmatically (jaxforms' address block). When it
 *    refuses, the field *captions themselves change* — which is what used to convince the walk
 *    it had advanced to a new step.
 * 3. *Angaben zur Veranstaltung* — an input whose framework owns the value and reverts anything
 *    assigned to it (the eGov wizard's React fields), and a page script that quietly tries to
 *    POST to `/submit` in the background, which nothing the walk decides can prevent.
 * 4. *Zusammenfassung* — no fields, and two buttons that both file the application. One of them
 *    starts with the word *Weiter*.
 *
 * `/submit` records every hit and never does anything else. `GET /__audit` reports those hits,
 * `POST /__reset` clears them: a test asserts against the *server's* record, not against the
 * walk's own account of what it pressed.
 *
 * Node standard library only, no dependencies, no persistence.
 */

import { createServer } from "node:http";

const PORT = Number(process.env.PORT || 3200);

/** Every hit on the endpoint that would file the application. Must stay empty during a walk. */
const submissions = [];

const TOWNS = ["Basel", "Bern", "Baden"];
const CATEGORIES = ["Strassenfest", "Markt", "Konzert"];

/** Answered as a whole; the mask is what a time field shows before anything is typed. */
const TIME_MASK = "__:__";

const html = (strings, ...values) => strings.reduce((out, part, i) => out + part + (values[i] ?? ""), "");

const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );

/**
 * One field, with its caption.
 *
 * The caption carries the error text when there is one, rather than the error living in a
 * separate element: that is what makes a refused step read as a *different* step to anything
 * fingerprinting a control by the words next to it, and reproducing it is half the point of
 * this fixture.
 */
const field = ({ id, label, error, control }) => html`
  <div class="field ${error ? "field--invalid" : ""}">
    <label for="${id}">${escape(label)}${error ? ` — ${escape(error)}` : ""}</label>
    ${control}
  </div>
`;

const page = ({ step, title, body, next = "Weiter", nextDisabled = false, scripts = "" }) => html`
  <!doctype html>
  <html lang="de">
    <head>
      <meta charset="utf-8" />
      <title>${escape(title)}</title>
      <style>
        body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem; }
        .field { margin: 1.2rem 0; }
        label { display: block; font-weight: 600; margin-bottom: 0.3rem; }
        input, select { font-size: 1rem; padding: 0.4rem; width: 20rem; }
        .field--invalid label { color: #b00020; }
        .field--invalid input { border: 2px solid #b00020; }
        [role="listbox"] { border: 1px solid #999; list-style: none; margin: 0; padding: 0; width: 20rem; }
        [role="option"] { padding: 0.3rem 0.5rem; cursor: pointer; }
        button[disabled] { opacity: 0.5; }
        nav ol { display: flex; gap: 1rem; list-style: none; padding: 0; }
      </style>
    </head>
    <body>
      <nav>
        <ol>
          ${[1, 2, 3, 4]
            .map(
              (index) =>
                `<li${index === step ? ' aria-current="step"' : ""}>Schritt ${index}</li>`,
            )
            .join("")}
        </ol>
      </nav>
      <h1>${escape(title)}</h1>
      <form method="post" action="/step/${step}">
        ${body}
        <button type="submit" id="next" ${nextDisabled ? "disabled" : ""}>${escape(next)}</button>
      </form>
      <script>
        ${scripts}
      </script>
    </body>
  </html>
`;

/** Step 1: a radio group the markup says nothing about, gating a disabled next button. */
const stepOne = () =>
  page({
    step: 1,
    title: "Art der Veranstaltung",
    nextDisabled: true,
    body: html`
      <fieldset>
        <legend>Wird öffentlicher Grund beansprucht?</legend>
        ${["Ja", "Nein", "Teilweise"]
          .map(
            (option) => html`
              <div>
                <input type="radio" id="art-${option}" name="art" value="${option}" />
                <label for="art-${option}">${option}</label>
              </div>
            `,
          )
          .join("")}
      </fieldset>
    `,
    // The button comes alive only once the group is answered, and only on a real change event —
    // the same shape as a form that validates as you go.
    //
    // The websocket is the other thing this step is for. A form framework that pushes over one
    // — Vaadin does — could send everything typed into the form without a single HTTP request,
    // and request interception never sees it. Opened here so that something has to refuse it.
    scripts: `
      document.querySelectorAll('input[name="art"]').forEach((radio) => {
        radio.addEventListener("change", () => { document.getElementById("next").disabled = false; });
      });

      try {
        const live = new WebSocket("ws://" + location.host + "/live");
        live.addEventListener("open", () => live.send("angaben"));
      } catch (error) {
        // A refused websocket is the expected outcome, not a failure of the page.
      }
    `,
  });

/**
 * Step 2: nothing marked required, and three controls that refuse to be filled the easy way.
 *
 * Errors arrive only as `values` carrying what the server refused, so a first render says
 * nothing about what it will demand.
 */
const stepTwo = (values = {}, errors = {}) =>
  page({
    step: 2,
    title: "Kontaktangaben",
    body: html`
      ${field({
        id: "vorname",
        label: "Vorname",
        error: errors.vorname,
        control: `<input id="vorname" name="vorname" value="${escape(values.vorname || "")}"${errors.vorname ? ' aria-invalid="true"' : ""} />`,
      })}
      ${field({
        id: "ort",
        label: "Ort",
        error: errors.ort,
        control: html`
          <input id="ort" name="ort" role="combobox" aria-autocomplete="list" aria-expanded="false"
                 autocomplete="off" value="${escape(values.ort || "")}"${errors.ort ? ' aria-invalid="true"' : ""} />
          <ul role="listbox" id="ort-list" hidden></ul>
        `,
      })}
      ${field({
        id: "plz",
        label: "PLZ",
        error: errors.plz,
        control: `<input id="plz" name="plz" maxlength="4" value="${escape(values.plz || "")}"${errors.plz ? ' aria-invalid="true"' : ""} />`,
      })}
      ${field({
        id: "beginn",
        label: "Beginn",
        error: errors.beginn,
        control: `<input id="beginn" name="beginn" value="${escape(values.beginn || TIME_MASK)}"${errors.beginn ? ' aria-invalid="true"' : ""} />`,
      })}
    `,
    scripts: `
      // A postcode that takes digits and silently drops everything else: "Test" reads back
      // empty, so a walk that writes a value and trusts it has written nothing.
      const plz = document.getElementById("plz");
      plz.addEventListener("input", () => { plz.value = plz.value.replace(/\\D/g, "").slice(0, 4); });

      // A time field that is never empty. It wears its mask, so anything asking "does this have
      // a value" is told yes while the server sees nothing.
      const beginn = document.getElementById("beginn");
      beginn.addEventListener("input", () => {
        const digits = beginn.value.replace(/\\D/g, "").slice(0, 4);
        beginn.value = digits.length > 2 ? digits.slice(0, 2) + ":" + digits.slice(2) : digits || ${JSON.stringify(TIME_MASK)};
      });

      // A town that only exists if it was picked from the list. Two separate refusals: a value
      // put in programmatically is wiped once the widget notices, and a typed value the widget
      // does not recognise is cleared when focus leaves.
      const towns = ${JSON.stringify(TOWNS)};
      const ort = document.getElementById("ort");
      const list = document.getElementById("ort-list");
      let picked = "";
      ort.addEventListener("input", (event) => {
        if (!event.isTrusted) { setTimeout(() => { ort.value = picked; }, 30); return; }
        const query = ort.value.toLowerCase();
        const hits = query ? towns.filter((town) => town.toLowerCase().startsWith(query)) : [];
        list.innerHTML = hits.map((town) => '<li role="option">' + town + "</li>").join("");
        list.hidden = hits.length === 0;
        ort.setAttribute("aria-expanded", String(hits.length > 0));
      });
      list.addEventListener("click", (event) => {
        if (event.target.getAttribute("role") !== "option") return;
        picked = event.target.textContent;
        ort.value = picked;
        list.hidden = true;
      });
      ort.addEventListener("blur", () => {
        setTimeout(() => {
          if (!towns.includes(ort.value)) ort.value = picked;
          list.hidden = true;
        }, 50);
      });
    `,
  });

/**
 * Step 3: a framework-owned input, a select, a stated-required consent box — and a script that
 * tries to file the application behind everyone's back.
 *
 * The background POST is the case no decision of the walk's can cover: nothing was clicked, so
 * nothing classified it. Only something watching the network stops it.
 */
const stepThree = (values = {}, errors = {}) =>
  page({
    step: 3,
    title: "Angaben zur Veranstaltung",
    body: html`
      ${field({
        id: "titel",
        label: "Titel der Veranstaltung",
        error: errors.titel,
        control: `<input id="titel" name="titel" value="${escape(values.titel || "")}"${errors.titel ? ' aria-invalid="true"' : ""} />`,
      })}
      ${field({
        id: "kategorie",
        label: "Kategorie",
        error: errors.kategorie,
        control: html`
          <select id="kategorie" name="kategorie">
            <option value="">Bitte wählen</option>
            ${CATEGORIES.map((category) => `<option value="${category}">${category}</option>`).join("")}
          </select>
        `,
      })}
      <div class="field">
        <input type="checkbox" id="agb" name="agb" required aria-required="true" />
        <label for="agb">Ich akzeptiere die Allgemeinen Geschäftsbedingungen</label>
      </div>
    `,
    scripts: `
      // The framework owns this value. An assignment from outside is reverted on the spot, so
      // the field looks filled to whoever wrote it and reads as empty to the server.
      const titel = document.getElementById("titel");
      let state = titel.value;
      titel.addEventListener("input", (event) => {
        if (event.isTrusted) { state = titel.value; return; }
        titel.value = state;
      });

      // Nobody pressed anything. This is what a label denylist cannot see.
      setTimeout(() => { fetch("/submit", { method: "POST", body: "background" }).catch(() => {}); }, 1500);
    `,
  });

/** Step 4: no fields, and two buttons that both file the application. */
const stepFour = () => html`
  <!doctype html>
  <html lang="de">
    <head>
      <meta charset="utf-8" />
      <title>Zusammenfassung</title>
    </head>
    <body>
      <nav><ol><li aria-current="step">Schritt 4</li></ol></nav>
      <h1>Zusammenfassung</h1>
      <p>Bitte prüfen Sie Ihre Angaben. Mit dem Absenden wird die Bewilligung kostenpflichtig beantragt.</p>
      <form method="post" action="/submit">
        <button type="submit" name="how" value="weiter">Weiter zur zahlungspflichtigen Bestellung</button>
        <button type="submit" name="how" value="bestellen">Kostenpflichtig bestellen</button>
      </form>
    </body>
  </html>
`;

/** What step 2 demands, discovered by the applicant only once it has refused. */
const validateTwo = (values) => {
  const errors = {};
  if (!values.vorname) errors.vorname = "Feld darf nicht leer sein";
  if (!TOWNS.includes(values.ort || "")) errors.ort = "Bitte einen Ort aus der Liste wählen";
  if (!/^\d{4}$/.test(values.plz || "")) errors.plz = "Bitte eine vierstellige PLZ eingeben";
  if (!/^\d{2}:\d{2}$/.test(values.beginn || "")) errors.beginn = "Bitte eine Uhrzeit eingeben";
  return errors;
};

/** What step 3 demands. The consent box is the only thing here the markup admits to. */
const validateThree = (values) => {
  const errors = {};
  if (!values.titel) errors.titel = "Feld darf nicht leer sein";
  if (!values.kategorie) errors.kategorie = "Bitte eine Kategorie wählen";
  if (!values.agb) errors.agb = "Bitte bestätigen";
  return errors;
};

const readBody = (request) =>
  new Promise((resolve) => {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => resolve(Object.fromEntries(new URLSearchParams(body))));
  });

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://localhost:${PORT}`);
  const send = (status, body, type = "text/html; charset=utf-8") => {
    response.writeHead(status, { "content-type": type });
    response.end(body);
  };

  // Filing the application. It does nothing but remember that it was reached, which is the
  // assertion every test here is really making.
  if (url.pathname === "/submit") {
    submissions.push({ method: request.method, at: new Date().toISOString(), body: await readBody(request) });
    send(200, "<!doctype html><html><body><h1>Eingereicht</h1></body></html>");
    return;
  }

  if (url.pathname === "/__audit") {
    send(200, JSON.stringify({ submissions }), "application/json");
    return;
  }

  if (url.pathname === "/__reset") {
    submissions.length = 0;
    send(200, JSON.stringify({ submissions }), "application/json");
    return;
  }

  if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/step/1")) {
    send(200, stepOne());
    return;
  }

  if (request.method === "POST" && url.pathname === "/step/1") {
    const values = await readBody(request);
    send(200, values.art ? stepTwo() : stepOne());
    return;
  }

  if (request.method === "POST" && url.pathname === "/step/2") {
    const values = await readBody(request);
    const errors = validateTwo(values);
    send(200, Object.keys(errors).length ? stepTwo(values, errors) : stepThree());
    return;
  }

  if (request.method === "POST" && url.pathname === "/step/3") {
    const values = await readBody(request);
    const errors = validateThree(values);
    send(200, Object.keys(errors).length ? stepThree(values, errors) : stepFour());
    return;
  }

  send(404, "<!doctype html><html><body>not found</body></html>");
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(JSON.stringify({ event: "wizard_fixture_started", port: PORT }));
});
