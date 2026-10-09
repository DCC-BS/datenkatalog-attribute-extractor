# Context

The language this project uses for reading forms. Glossary only — no implementation detail,
no decisions, no plans.

## Form

A collection of fields an authority asks a person to answer. It may be a PDF, a web page, or a
sequence of web pages. What we produce from it is an **Inventory**, never an answer to it.

## Inventory

The list of fields a Form asks for: each field's label, its enclosing headings, its name. The
deliverable. An Inventory that covers part of a Form and says so is usable; one that covers
part of a Form silently is a wrong answer.

## Step

One stage of a Form that reveals its fields a stage at a time. A Step is a unit of work: it is
observed, photographed, and either advanced past or stopped on. Two renders of the same Step —
one blank, one carrying validation errors — are the *same* Step.

## Walk

The traversal of a Form's Steps: observe a Step, answer enough of it, Advance, repeat. A Walk
ends for exactly one stated reason.

## Advance

A move from one Step to the next. Reversible, and creates nothing at the authority. On a
server-validated Form an Advance necessarily carries the answers given so far to the server —
that is accepted. Advancing is the only reason this project ever puts a value into a Form.

## Submit

The act that hands the Form to the authority: a case opened, an order placed, a report filed,
a payment owed. Irreversible, and outside anything a reader of a Form is entitled to do.

**This project must never Submit.** The distinction from Advance is the load-bearing one in
the whole domain: both are a button press on a wizard, and only one of them is allowed.

## Point of no return

The last Step of a Walk that can be reached without Submitting — typically a summary of what
was answered, whose forward button is a Submit however it is worded. Reaching it is a
*successful* end of a Walk, not a failure.

## Answering

Putting a value into a control so that a Step will Advance. The values are placeholders; they
mean nothing and are chosen only to satisfy a validator. Answering is never data entry.

## Pristine

A Step as it renders before this project has Answered anything on it — no placeholder values,
no validation errors. What the model is shown of a Step must be Pristine: an image of a Step
scolding itself over empty fields is not a picture of the Form.

## Blocked

A Step that refused to Advance. A Walk that stops Blocked names the controls the Step would
not accept, because "it would not advance" is only actionable with "because of these".
