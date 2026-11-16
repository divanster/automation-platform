import { describe, expect, it } from "vitest";

import type { Draft } from "../api/types";
import {
  addItem,
  displayedPaths,
  fieldErrorPathToFindingPath,
  fieldPayload,
  formFromDraft,
  groupByPath,
  isDirty,
  itemPath,
  payloadFromForm,
  removeItem,
  setItemField,
  setTopField,
} from "./draftForm";

const base = (): Draft => ({
  requester: { name: "Synthetic Requester", contact: null },
  items: [
    { description: "First", quantity: "2.50", unit: "pcs", notes: null },
    { description: "Second", quantity: "10", unit: null, notes: "" },
    { description: "Third", quantity: "0.1000000000000000000001", unit: "kg", notes: "line1\r\nline2" },
  ],
  notes: null,
  source_refs: [
    { field_path: "requester.name", excerpt: "Requester: Synthetic Requester", line: 1 },
    { field_path: "items[0].description", excerpt: "Item: 2.50 | pcs | First", line: 2 },
    { field_path: "items[1].quantity", excerpt: "Item: 10 | | Second", line: 3 },
    { field_path: "items[2]", excerpt: "Item: 0.1 | kg | Third", line: 4 },
    { field_path: "notes", excerpt: "Notes: x", line: null },
  ],
});

describe("initialization and exact preservation", () => {
  it("shows empty inputs for missing values and the text for present ones", () => {
    const form = formFromDraft(base());
    expect(form.requesterName.value).toBe("Synthetic Requester");
    expect(form.requesterContact.value).toBe("");
    expect(form.items.map((i) => i.quantity.value)).toEqual(["2.50", "10", "0.1000000000000000000001"]);
  });

  it("sends an untouched draft back exactly as received", () => {
    const draft = base();
    expect(payloadFromForm(formFromDraft(draft))).toEqual(draft);
  });

  it("keeps null as null and an empty string as an empty string when the field is not touched", () => {
    const payload = payloadFromForm(formFromDraft(base()));
    expect(payload.requester.contact).toBeNull();
    expect(payload.items[1]?.unit).toBeNull();
    expect(payload.items[1]?.notes).toBe("");
    expect(payload.notes).toBeNull();
  });

  it("keeps decimal quantities as the exact strings, never as numbers", () => {
    const payload = payloadFromForm(formFromDraft(base()));
    expect(payload.items.map((i) => i.quantity)).toEqual(["2.50", "10", "0.1000000000000000000001"]);
    for (const item of payload.items) expect(typeof item.quantity).toBe("string");
  });

  it("sends an edited quantity as the typed text", () => {
    let form = formFromDraft(base());
    form = setItemField(form, form.items[0]!.key, "quantity", "12345678901234567890.123456789");
    expect(payloadFromForm(form).items[0]?.quantity).toBe("12345678901234567890.123456789");
  });

  it("does not trim or reformat what the user typed", () => {
    let form = formFromDraft(base());
    form = setTopField(form, "requesterName", "  spaced   name  ");
    expect(payloadFromForm(form).requester.name).toBe("  spaced   name  ");
  });

  it("sends null, not an invented default, for a field the user emptied", () => {
    let form = formFromDraft(base());
    form = setTopField(form, "requesterName", "");
    form = setItemField(form, form.items[0]!.key, "quantity", "");
    const payload = payloadFromForm(form);
    expect(payload.requester.name).toBeNull();
    expect(payload.items[0]?.quantity).toBeNull();
  });

  it("treats a textarea's LF as equal to the original CRLF, so an untouched field keeps its exact original", () => {
    const form = formFromDraft(base());
    const third = form.items[2]!;
    const reported = setItemField(form, third.key, "notes", "line1\nline2"); // what a textarea reports for the same text
    expect(payloadFromForm(reported).items[2]?.notes).toBe("line1\r\nline2");
    expect(isDirty(reported)).toBe(false);
  });

  it("sends exactly the keys the API accepts", () => {
    const payload = payloadFromForm(formFromDraft(base()));
    expect(Object.keys(payload).sort()).toEqual(["items", "notes", "requester", "source_refs"]);
    expect(Object.keys(payload.requester).sort()).toEqual(["contact", "name"]);
    expect(Object.keys(payload.items[0]!).sort()).toEqual(["description", "notes", "quantity", "unit"]);
    expect(Object.keys(payload.source_refs[0]!).sort()).toEqual(["excerpt", "field_path", "line"]);
  });

  it("preserves source references unchanged, including a null line", () => {
    const draft = base();
    expect(payloadFromForm(formFromDraft(draft)).source_refs).toEqual(draft.source_refs);
  });

  it("copies the draft: later changes to the form never alter the original object", () => {
    const draft = base();
    let form = formFromDraft(draft);
    form = removeItem(form, 0);
    expect(draft.items).toHaveLength(3);
    expect(draft.source_refs).toHaveLength(5);
  });
});

describe("dirty tracking", () => {
  it("is clean after loading and dirty after a change, and clean again when the change is reverted", () => {
    let form = formFromDraft(base());
    expect(isDirty(form)).toBe(false);
    form = setTopField(form, "requesterName", "Other");
    expect(isDirty(form)).toBe(true);
    form = setTopField(form, "requesterName", "Synthetic Requester");
    expect(isDirty(form)).toBe(false);
  });

  it("is dirty after an item is added or removed", () => {
    expect(isDirty(addItem(formFromDraft(base())))).toBe(true);
    expect(isDirty(removeItem(formFromDraft(base()), 0))).toBe(true);
  });
});

describe("adding and removing items", () => {
  it("adds an empty item with no values and no source references", () => {
    const form = addItem(formFromDraft(base()));
    expect(form.items).toHaveLength(4);
    expect(payloadFromForm(form).items[3]).toEqual({ description: null, quantity: null, unit: null, notes: null });
    expect(form.sourceRefs).toHaveLength(5);
  });

  it("removing an item drops its references and renumbers the later ones", () => {
    const form = removeItem(formFromDraft(base()), 1);
    expect(form.items.map((i) => i.description.value)).toEqual(["First", "Third"]);
    expect(form.sourceRefs.map((r) => r.field_path)).toEqual(["requester.name", "items[0].description", "items[1]", "notes"]);
    expect(form.droppedRefs).toBe(1);
  });

  it("leaves references to earlier items and to non-item fields alone", () => {
    const form = removeItem(formFromDraft(base()), 2);
    expect(form.sourceRefs.map((r) => r.field_path)).toEqual(["requester.name", "items[0].description", "items[1].quantity", "notes"]);
  });

  it("ignores an index that does not exist", () => {
    const form = formFromDraft(base());
    expect(removeItem(form, 9)).toBe(form);
    expect(removeItem(form, -1)).toBe(form);
  });

  it("renumbers multi-digit references correctly", () => {
    const draft: Draft = {
      requester: { name: "n", contact: null },
      items: Array.from({ length: 12 }, (_, i) => ({ description: `d${i}`, quantity: "1", unit: null, notes: null })),
      notes: null,
      source_refs: [{ field_path: "items[11].quantity", excerpt: "x", line: 1 }],
    };
    const form = removeItem(formFromDraft(draft), 0);
    expect(form.sourceRefs[0]?.field_path).toBe("items[10].quantity");
  });
});

describe("findings beside fields", () => {
  it("groups findings by their field path", () => {
    const grouped = groupByPath([
      { code: "a", field_path: "requester.name", blocking: true, message: "m1" },
      { code: "b", field_path: "requester.name", blocking: false, message: "m2" },
      { code: "c", field_path: itemPath(0, "quantity"), blocking: true, message: "m3" },
    ]);
    expect(grouped.get("requester.name")).toHaveLength(2);
    expect(grouped.get("items[0].quantity")).toHaveLength(1);
  });

  it("knows which paths the form can show, so other findings are listed instead of dropped", () => {
    const paths = displayedPaths(formFromDraft(base()));
    for (const path of ["requester.name", "requester.contact", "notes", "items", "items[2]", "items[2].quantity"]) expect(paths.has(path)).toBe(true);
    expect(paths.has("items[3]")).toBe(false);
    expect(paths.has("source_refs[0]")).toBe(false);
  });

  it("maps the transport error paths onto finding paths", () => {
    expect(fieldErrorPathToFindingPath("draft.items[0].quantity")).toBe("items[0].quantity");
    expect(fieldErrorPathToFindingPath("draft.requester.name")).toBe("requester.name");
    expect(fieldErrorPathToFindingPath("base_revision_id")).toBe("base_revision_id");
  });
});

describe("fieldPayload", () => {
  it("returns the original for an unchanged field and the typed text for a changed one", () => {
    expect(fieldPayload({ value: "", original: null })).toBeNull();
    expect(fieldPayload({ value: "", original: "" })).toBe("");
    expect(fieldPayload({ value: "x", original: null })).toBe("x");
    expect(fieldPayload({ value: "", original: "was here" })).toBeNull();
  });
});
