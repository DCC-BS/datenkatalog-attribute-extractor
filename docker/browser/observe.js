/**
 * Observes the form controls of a rendered page the way a person reading it would.
 *
 * This runs inside the page, after it has rendered, and is the reason the browser is ours.
 * Every association a form relies on is a *visual* one: a caption is the text beside or above
 * the box you type into, a section heading is the larger, heavier text above a run of fields.
 * HTML can state those relationships — `label for`, `fieldset`, `h1`-`h6` — and where it does,
 * that statement is believed. Most generated forms state none of them, and reading the DOM
 * shape instead (sibling order, nesting depth, tag names, inline styles) produces rules that
 * fit the one page they were written against and break on the next toolkit.
 *
 * So the fallbacks here are geometric and typographic, not structural:
 *
 *   * A caption is the nearest text to the left of a control on the same line, else the
 *     nearest text directly above it, else — for a tick box — the nearest text to its right.
 *     Nearest wins, and a piece of text captions at most one control.
 *   * A heading is text set larger or heavier than the page's body text, with fields below it
 *     and no field beside it. Levels come from ranking those sizes against each other, so a
 *     page that never uses an `<h1>` still yields a heading chain.
 *
 * Both rules are stated in pixels and computed styles, which every page has, rather than in
 * markup conventions, which only some pages follow.
 */
(options) => {
  const {
    maxOptions,
    maxTextLength,
    maxHeadingLength,
    minControlSize,
    captionMaxLeftGap,
    captionMaxAboveGap,
    captionMaxRightGap,
  } = options;

  // Ranking the three caption positions against each other. A label to the left is the
  // overwhelmingly common case, one above is next, and one to the right is the tick box. The
  // penalties keep a slightly closer text in an unusual position from beating the obvious one.
  const ABOVE_PENALTY = 120;
  const RIGHT_PENALTY = 60;

  // How far up from a control the search for its rendered box may go.
  const MAX_BOX_ANCESTORS = 3;

  // Attribute left on the element whose box a control occupies, carrying that control's index
  // in the returned inventory. It is how a screenshot tile is told which controls it shows:
  // the page is photographed screen by screen, and after each scroll the marked elements that
  // intersect the viewport are exactly the controls on that screen. Doing it this way rather
  // than from the boxes reported here is what makes it survive an inner scrolling pane, where
  // a control's document coordinates and the tile's scroll offset are in different spaces.
  const BOX_MARKER = "data-obs-index";

  // Text nodes closer than this on the same line are one caption. `Ich habe die <a>Wegleitung</a>
  // gelesen` is three nodes and one sentence, and a form's labels are full of such links,
  // abbreviations and asterisks.
  const RUN_MERGE_MAX_GAP = 8;

  // Heading sizes within this many pixels of each other are the same level. Section titles on
  // one page routinely differ by a pixel through inherited styling without differing in rank.
  const HEADING_SIZE_TOLERANCE = 2;

  const NATIVE_SELECTOR = "input, select, textarea";
  const EDITABLE_SELECTOR = '[contenteditable=""], [contenteditable="true"]';

  // Controls a person cannot type into, or that submit rather than collect.
  const SKIPPED_TYPES = new Set(["hidden", "submit", "button", "reset", "image"]);

  // A control need not be an `<input>`. A field filled in as a table, a custom dropdown, a
  // div tick box — none of them hold a native control, and all of them say what they are
  // through their ARIA role, which is a standard rather than a toolkit's private convention.
  const WIDGET_ROLE_KINDS = {
    grid: "tabelle",
    treegrid: "tabelle",
    combobox: "auswahlliste",
    listbox: "auswahlliste",
    textbox: "text",
    searchbox: "text",
    spinbutton: "zahl",
    slider: "schieberegler",
    switch: "schalter",
    checkbox: "checkbox",
    radio: "radio",
    radiogroup: "optionsgruppe",
  };

  const WIDGET_SELECTOR = Object.keys(WIDGET_ROLE_KINDS)
    .map((role) => `[role="${role}"]`)
    .join(", ");

  const INTERACTIVE_SELECTOR =
    'a[href], button, summary, [role="link"], [role="button"], [role="menuitem"], [role="tab"], [role="option"]';

  const LANDMARK_SELECTOR = 'nav, header, footer, [role="navigation"], [role="banner"], [role="contentinfo"]';

  const clean = (value) =>
    (value || "")
      // Icon fonts draw their glyphs from the Unicode private use area. They read as text and
      // carry none, so a control beside an icon would otherwise be captioned with one.
      .replace(/[\uE000-\uF8FF]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maxTextLength);

  const boxOf = (rect) => ({
    x: rect.left + window.scrollX,
    y: rect.top + window.scrollY,
    w: rect.width,
    h: rect.height,
  });

  const right = (box) => box.x + box.w;
  const bottom = (box) => box.y + box.h;

  const overlap = (aStart, aEnd, bStart, bEnd) => Math.max(0, Math.min(aEnd, bEnd) - Math.max(aStart, bStart));

  /**
   * The space a control occupies on the page, or null if it occupies none.
   *
   * An element frequently is not its own rendered extent. A virtualised grid is a zero-height
   * `<table>` inside the box a person sees; a styled file upload is an invisible `<input>`
   * behind the button that stands in for it. Both are ordinary, both appear on this one page,
   * and in both cases the field's real box belongs to an ancestor — so the nearest ancestor
   * with an extent is used. What is genuinely not rendered, `display: none`, has no
   * `offsetParent` and is skipped: a collapsed section is not a field until it is opened.
   *
   * The node the box was taken from is returned alongside it. The caller marks that node, not
   * the control, so that asking later "is this control on this screen?" is a question about
   * something that has an extent — a zero-height `<input>` intersects no screen at all.
   */
  const renderedBox = (element) => {
    if (element.offsetParent === null && window.getComputedStyle(element).position !== "fixed") return null;

    let node = element;
    for (let step = 0; step <= MAX_BOX_ANCESTORS && node; step += 1) {
      const rect = node.getBoundingClientRect();
      if (rect.width >= minControlSize && rect.height >= minControlSize) return { box: boxOf(rect), node };
      node = node.parentElement;
    }
    return null;
  };

  /** The block-level element a piece of text is laid out in — the owner of its line box. */
  const blockOf = (element) => {
    let node = element;
    while (node && node !== document.body) {
      if (!window.getComputedStyle(node).display.startsWith("inline")) return node;
      node = node.parentElement;
    }
    return document.body;
  };

  const weightOf = (value) => {
    if (value === "bold" || value === "bolder") return 700;
    if (value === "normal" || value === "lighter") return 400;
    const parsed = parseInt(value, 10);
    return Number.isNaN(parsed) ? 400 : parsed;
  };

  const median = (values) => {
    if (values.length === 0) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  };

  /** The label the markup itself states, in the order the accessible name is computed. */
  const statedLabel = (element) => {
    if (element.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      const text = explicit ? clean(explicit.innerText || explicit.textContent) : "";
      if (text) return text;
    }

    const wrapping = element.closest("label");
    if (wrapping) {
      const text = clean(wrapping.innerText || wrapping.textContent);
      if (text) return text;
    }

    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = clean(
        labelledBy
          .split(/\s+/)
          .map((id) => document.getElementById(id))
          .filter(Boolean)
          .map((node) => node.innerText || node.textContent)
          .join(" "),
      );
      if (text) return text;
    }

    for (const attribute of ["aria-label", "title", "placeholder"]) {
      const text = clean(element.getAttribute(attribute));
      if (text) return text;
    }

    return "";
  };

  const nativeKind = (element, type) => {
    if (element.tagName === "SELECT") return "auswahlliste";
    if (element.tagName === "TEXTAREA") return "textfeld mehrzeilig";
    return type;
  };

  /**
   * What a control offers to choose from: a dropdown's options, or the rows of a grid.
   *
   * A grid the applicant fills in row by row is one field; its rows say what that field is
   * for, exactly as a dropdown's options do, and are reported the same way.
   */
  const optionsOf = (element) => {
    const texts = [];
    const push = (value) => {
      const text = clean(value);
      if (text && !texts.includes(text)) texts.push(text);
    };

    if (element.tagName === "SELECT") {
      for (const option of element.querySelectorAll("option")) push(option.textContent);
    }
    for (const option of element.querySelectorAll('[role="option"]')) push(option.textContent);
    for (const row of element.querySelectorAll('[role="row"]')) {
      const cell = row.querySelector('[role="gridcell"], td');
      if (cell) push(cell.textContent);
    }

    return texts.slice(0, maxOptions);
  };

  const records = new Map();

  // A page is observed once per step of a wizard, in the same document. Marks left by the
  // previous step would otherwise still be there, pointing at indices of an inventory that no
  // longer exists.
  for (const marked of document.querySelectorAll(`[${BOX_MARKER}]`)) marked.removeAttribute(BOX_MARKER);

  const addControl = (element, kind) => {
    if (records.has(element)) return;
    const rendered = renderedBox(element);
    if (!rendered) return;
    records.set(element, {
      kind,
      name: clean(element.getAttribute("name")),
      stated_label: statedLabel(element),
      caption: "",
      options: optionsOf(element),
      in_form: element.closest("form") !== null,
      box: rendered.box,
      boxNode: rendered.node,
    });
  };

  for (const element of document.querySelectorAll(NATIVE_SELECTOR)) {
    const type = (element.getAttribute("type") || "text").toLowerCase();
    if (element.tagName === "INPUT" && SKIPPED_TYPES.has(type)) continue;
    addControl(element, nativeKind(element, type));
  }

  for (const element of document.querySelectorAll(WIDGET_SELECTOR)) {
    // A role wrapping a native control is that control's chrome, not a second field, and a
    // role nested in another widget is part of that widget.
    if (element.querySelector(NATIVE_SELECTOR)) continue;
    if (element.parentElement && element.parentElement.closest(WIDGET_SELECTOR)) continue;
    addControl(element, WIDGET_ROLE_KINDS[element.getAttribute("role").toLowerCase()]);
  }

  for (const element of document.querySelectorAll(EDITABLE_SELECTOR)) {
    addControl(element, "textfeld mehrzeilig");
  }

  const controlElements = [...records.keys()];
  const controls = controlElements.map((element) => records.get(element));

  // Text that belongs to a control — a grid's cells, a dropdown's options — describes that
  // control and must not be offered as another one's caption or as a heading.
  const insideControl = (element) => controlElements.some((control) => control.contains(element));

  const pieces = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const text = clean(node.nodeValue);
    if (!text) continue;

    const parent = node.parentElement;
    if (!parent || parent.closest("script, style, noscript") || insideControl(parent)) continue;

    const range = document.createRange();
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;

    const style = window.getComputedStyle(parent);
    pieces.push({
      text,
      box: boxOf(rect),
      fontSize: parseFloat(style.fontSize) || 0,
      fontWeight: weightOf(style.fontWeight),
      // A menu item is styled exactly like a section title — larger, heavier, above the
      // fields — and a documentation site's sidebar is full of them. What separates them is
      // not how they look but what they are: something you click, or a landmark the page
      // itself marks as navigation rather than content.
      interactive: parent.closest(INTERACTIVE_SELECTOR) !== null,
      landmark: parent.closest(LANDMARK_SELECTOR) !== null,
      // Two texts belong to the same line only inside the same block. Without this, a label
      // at the right edge of a column merges with whatever the next column starts with.
      block: blockOf(parent),
    });
  }

  // One caption is often several text nodes — a link, an abbreviation, a required asterisk.
  // Pieces that sit on the same line within a few pixels of each other are joined back into
  // the sentence a person reads, and the joined run takes the typography of its longest part.
  pieces.sort((a, b) => a.box.y - b.box.y || a.box.x - b.box.x);

  const runs = [];
  for (const piece of pieces) {
    const previous = runs[runs.length - 1];
    const sameLine =
      previous &&
      overlap(previous.box.y, bottom(previous.box), piece.box.y, bottom(piece.box)) /
        Math.max(1, Math.min(previous.box.h, piece.box.h)) >=
        0.5;
    const adjacent = previous && piece.box.x - right(previous.box) <= RUN_MERGE_MAX_GAP;

    if (sameLine && adjacent && previous.block === piece.block) {
      if (piece.text.length > previous.longest) {
        previous.longest = piece.text.length;
        previous.fontSize = piece.fontSize;
        previous.fontWeight = piece.fontWeight;
      }
      previous.text = clean(`${previous.text} ${piece.text}`);
      // A sentence containing a link is still a caption; a run made only of links is a menu.
      previous.interactive = previous.interactive && piece.interactive;
      previous.landmark = previous.landmark || piece.landmark;
      previous.box = {
        x: Math.min(previous.box.x, piece.box.x),
        y: Math.min(previous.box.y, piece.box.y),
        w: Math.max(right(previous.box), right(piece.box)) - Math.min(previous.box.x, piece.box.x),
        h: Math.max(bottom(previous.box), bottom(piece.box)) - Math.min(previous.box.y, piece.box.y),
      };
      continue;
    }

    runs.push({ ...piece, longest: piece.text.length, heading: false, used: false });
  }

  const bodySize = median(runs.map((run) => run.fontSize));
  const bodyWeight = median(runs.map((run) => run.fontWeight));

  /** Whether a control sits on this text's own line, which makes it a caption rather than a heading. */
  const captionsAControl = (run) =>
    controls.some((control) => {
      const rowOverlap = overlap(run.box.y, bottom(run.box), control.box.y, bottom(control.box));
      const gap = control.box.x - right(run.box);
      return rowOverlap / Math.max(1, Math.min(run.box.h, control.box.h)) >= 0.5 && gap >= -2 && gap <= captionMaxLeftGap;
    });

  for (const run of runs) {
    if (run.interactive || run.landmark) continue;
    const emphasised = run.fontSize > bodySize * 1.05 || run.fontWeight > bodyWeight;
    const hasFieldsBelow = controls.some((control) => control.box.y >= bottom(run.box) - 2);
    run.heading = emphasised && hasFieldsBelow && run.text.length <= maxHeadingLength && !captionsAControl(run);
  }

  // Levels come from the sizes the page actually uses: the largest, heaviest heading is level
  // one, the next distinct pair level two, and so on. A page that styles every section title
  // identically therefore gets one flat level rather than an invented hierarchy.
  // Sizes are clustered before they are ranked, and weight is collapsed to "bold or not", so
  // two section titles that differ by a pixel or by 600 against 700 stay one level instead of
  // nesting one inside the other. Only a visible step in size or weight makes a new level.
  const headings = runs.filter((run) => run.heading);
  const sizes = [...new Set(headings.map((run) => run.fontSize))].sort((a, b) => b - a);
  const clusters = [];
  for (const size of sizes) {
    const last = clusters[clusters.length - 1];
    if (last && last[0] - size <= HEADING_SIZE_TOLERANCE) last.push(size);
    else clusters.push([size]);
  }

  const tierOf = (run) => {
    const cluster = clusters.findIndex((sizes) => sizes.includes(run.fontSize));
    return cluster * 2 + (run.fontWeight >= 600 ? 0 : 1);
  };

  const tiers = [...new Set(headings.map(tierOf))].sort((a, b) => a - b);
  for (const run of headings) run.level = tiers.indexOf(tierOf(run)) + 1;

  const sharesRow = (a, b) => overlap(a.y, bottom(a), b.y, bottom(b)) / Math.max(1, Math.min(a.h, b.h)) >= 0.5;
  const sharesColumn = (a, b) => overlap(a.x, right(a), b.x, right(b)) / Math.max(1, Math.min(a.w, b.w)) >= 0.3;

  /**
   * Whether another field stands between this text and this control.
   *
   * This, not a distance limit, is what stops a label being read across a two-column layout:
   * *Vorname* and *Nachname* sit on one line with a field after each, and what disqualifies
   * *Vorname* from labelling the second field is the first field in between — not how many
   * pixels away it is. A distance limit has to be guessed, and guessed wrongly it drops a
   * caption on a wide page and steals one on a narrow layout.
   */
  const fieldInBetween = (runBox, control, horizontal) =>
    controls.some((other) => {
      if (other === control) return false;
      if (horizontal) {
        return sharesRow(other.box, control.box) && other.box.x >= right(runBox) - 2 && right(other.box) <= control.box.x + 2;
      }
      return sharesColumn(other.box, control.box) && other.box.y >= bottom(runBox) - 2 && bottom(other.box) <= control.box.y + 2;
    });

  // Captions: nearest text wins, and each text captions at most one control, so a column of
  // labels beside a column of fields pairs up rather than collapsing onto one label.
  const pairs = [];
  for (const control of controls) {
    if (control.stated_label) continue;
    for (const run of runs) {
      // A heading is not a caption, and neither is a menu: text that is nothing but links is
      // navigation, wherever it sits on the page. A caption *containing* a link — "Ich habe
      // die <a>Wegleitung</a> gelesen" — is not, and survives, because merging only marks a
      // run interactive when every one of its parts was.
      if (run.heading || run.interactive) continue;

      const inRow = sharesRow(run.box, control.box);
      const inColumn = sharesColumn(run.box, control.box);

      const leftGap = control.box.x - right(run.box);
      const aboveGap = control.box.y - bottom(run.box);
      const rightGap = run.box.x - right(control.box);

      let score = null;
      if (inRow && leftGap >= -2 && leftGap <= captionMaxLeftGap && !fieldInBetween(run.box, control, true)) {
        score = leftGap;
      } else if (inColumn && aboveGap >= -2 && aboveGap <= captionMaxAboveGap && !fieldInBetween(run.box, control, false)) {
        score = aboveGap + ABOVE_PENALTY;
      } else if (inRow && rightGap >= -2 && rightGap <= captionMaxRightGap) {
        score = rightGap + RIGHT_PENALTY;
      }

      if (score !== null) pairs.push({ control, run, score });
    }
  }

  pairs.sort((a, b) => a.score - b.score);
  for (const { control, run } of pairs) {
    if (control.caption || run.used) continue;
    control.caption = run.text;
    run.used = true;
  }

  // The heading chain is read in reading order: headings and controls sorted top to bottom,
  // a stack that pops back to the level of each heading met.
  const items = [
    ...headings.map((run) => ({ kind: "heading", y: run.box.y, x: run.box.x, run })),
    ...controls.map((control) => ({ kind: "control", y: control.box.y, x: control.box.x, control })),
  ].sort((a, b) => a.y - b.y || a.x - b.x);

  const stack = [];
  for (const item of items) {
    if (item.kind === "heading") {
      while (stack.length && stack[stack.length - 1].level >= item.run.level) stack.pop();
      stack.push({ level: item.run.level, text: item.run.text });
      continue;
    }
    item.control.context_path = stack.map((entry) => entry.text);
  }

  // Marked last, so an index always refers to the inventory this call is about to return.
  controls.forEach((control, index) => control.boxNode.setAttribute(BOX_MARKER, String(index)));

  return {
    title: clean(document.title),
    text: (document.body ? document.body.innerText || "" : "").slice(0, 20000),
    page_height: Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0,
    ),
    controls: controls.map((control, index) => ({
      index,
      kind: control.kind,
      label: control.stated_label || control.caption,
      label_source: control.stated_label ? "markup" : control.caption ? "layout" : "",
      context_path: control.context_path || [],
      name: control.name,
      options: control.options,
      in_form: control.in_form,
    })),
  };
};
