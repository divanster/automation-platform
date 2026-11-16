// The editable form model for a draft, and the conversion back to the API's draft shape. Pure functions, no React.
//
// WHAT THIS DOES NOT DO. No validation and no business rule: whether a quantity is acceptable, whether a value is
// required, and what a finding means are decided by the server on every save. The form only keeps what the user typed,
// and sends back exactly what the API's `draft` shape allows.
//
// PRESERVATION. A field the user did not change is sent back EXACTLY as the server sent it (including `null`, an empty
// string, a decimal quantity such as "2.50", and text with "\r\n"). A changed field is sent as typed; a changed field
// that is empty is sent as `null` (no value), never as a made-up default. Nothing is trimmed or reformatted.

import type { Draft, DraftItem, DraftSourceRef, Finding } from "../api/types";

export interface FormField {
  value: string;
  /** The value the server sent: a string, or null when the field had no value. Never changes after loading. */
  original: string | null;
}

export interface ItemForm {
  /** Stable React key; unrelated to the item's position, which changes when an item is removed. */
  key: string;
  description: FormField;
  quantity: FormField;
  unit: FormField;
  notes: FormField;
}

export type ItemFieldName = "description" | "quantity" | "unit" | "notes";
export type TopFieldName = "requesterName" | "requesterContact" | "notes";

export interface DraftForm {
  requesterName: FormField;
  requesterContact: FormField;
  notes: FormField;
  items: ItemForm[];
  /** Source references, sent back unchanged except that removing an item drops and renumbers its references. */
  sourceRefs: DraftSourceRef[];
  /** True once an item was added or removed. */
  structureChanged: boolean;
  /** How many references were dropped because their item was removed. */
  droppedRefs: number;
}

let keyCounter = 0;
const nextKey = () => `item-${(keyCounter += 1)}`;

const field = (original: string | null): FormField => ({ value: original ?? "", original });

function itemForm(item: DraftItem | null): ItemForm {
  return {
    key: nextKey(),
    description: field(item?.description ?? null),
    quantity: field(item?.quantity ?? null),
    unit: field(item?.unit ?? null),
    notes: field(item?.notes ?? null),
  };
}

/** A `<textarea>` reports line breaks as "\n", so compare without the difference between "\r\n" and "\n". */
const unifyLineBreaks = (text: string) => text.replace(/\r\n?/g, "\n");

export function formFromDraft(draft: Draft): DraftForm {
  return {
    requesterName: field(draft.requester.name),
    requesterContact: field(draft.requester.contact),
    notes: field(draft.notes),
    items: draft.items.map((item) => itemForm(item)),
    sourceRefs: draft.source_refs.map((ref) => ({ field_path: ref.field_path, excerpt: ref.excerpt, line: ref.line })),
    structureChanged: false,
    droppedRefs: 0,
  };
}

export function fieldChanged(f: FormField): boolean {
  return unifyLineBreaks(f.value) !== unifyLineBreaks(f.original ?? "");
}

/** The value to send: the original when untouched, else the typed text, or null when it was emptied. */
export function fieldPayload(f: FormField): string | null {
  if (!fieldChanged(f)) return f.original;
  return f.value === "" ? null : f.value;
}

export function isDirty(form: DraftForm): boolean {
  if (form.structureChanged) return true;
  const fields = [form.requesterName, form.requesterContact, form.notes];
  for (const item of form.items) fields.push(item.description, item.quantity, item.unit, item.notes);
  return fields.some(fieldChanged);
}

export function payloadFromForm(form: DraftForm): Draft {
  return {
    requester: { name: fieldPayload(form.requesterName), contact: fieldPayload(form.requesterContact) },
    items: form.items.map((item) => ({
      description: fieldPayload(item.description),
      quantity: fieldPayload(item.quantity),
      unit: fieldPayload(item.unit),
      notes: fieldPayload(item.notes),
    })),
    notes: fieldPayload(form.notes),
    source_refs: form.sourceRefs.map((ref) => ({ field_path: ref.field_path, excerpt: ref.excerpt, line: ref.line })),
  };
}

export function setTopField(form: DraftForm, name: TopFieldName, value: string): DraftForm {
  return { ...form, [name]: { ...form[name], value } };
}

export function setItemField(form: DraftForm, key: string, name: ItemFieldName, value: string): DraftForm {
  return { ...form, items: form.items.map((item) => (item.key === key ? { ...item, [name]: { ...item[name], value } } : item)) };
}

/** Append an empty item. It has no source references and no values: the server reports what is missing. */
export function addItem(form: DraftForm): DraftForm {
  return { ...form, items: [...form.items, itemForm(null)], structureChanged: true };
}

const ITEM_REF = /^items\[(0|[1-9][0-9]*)\](\..+)?$/;

/**
 * Remove the item at `index`. A source reference names an item by position, and the server refuses a reference to an item
 * that does not exist, so the removed item's references are dropped and the later items' references are renumbered.
 */
export function removeItem(form: DraftForm, index: number): DraftForm {
  if (index < 0 || index >= form.items.length) return form;
  let dropped = 0;
  const sourceRefs: DraftSourceRef[] = [];
  for (const ref of form.sourceRefs) {
    const match = ITEM_REF.exec(ref.field_path);
    if (!match) {
      sourceRefs.push(ref);
      continue;
    }
    const position = Number(match[1]);
    if (position === index) {
      dropped += 1;
    } else if (position > index) {
      sourceRefs.push({ ...ref, field_path: `items[${position - 1}]${match[2] ?? ""}` });
    } else {
      sourceRefs.push(ref);
    }
  }
  return {
    ...form,
    items: form.items.filter((_, i) => i !== index),
    sourceRefs,
    structureChanged: true,
    droppedRefs: form.droppedRefs + dropped,
  };
}

// --- findings beside fields -------------------------------------------------------------------------------------

export const TOP_FIELD_PATHS: Record<TopFieldName, string> = {
  requesterName: "requester.name",
  requesterContact: "requester.contact",
  notes: "notes",
};

export function itemPath(index: number, name?: ItemFieldName): string {
  return name ? `items[${index}].${name}` : `items[${index}]`;
}

export type PathFinding = Pick<Finding, "code" | "field_path" | "blocking" | "message">;

export function groupByPath(findings: readonly PathFinding[]): Map<string, PathFinding[]> {
  const grouped = new Map<string, PathFinding[]>();
  for (const finding of findings) {
    const list = grouped.get(finding.field_path);
    if (list) list.push(finding);
    else grouped.set(finding.field_path, [finding]);
  }
  return grouped;
}

/** Every path the form shows a place for. Findings on any other path are listed separately, never dropped. */
export function displayedPaths(form: DraftForm): Set<string> {
  const paths = new Set<string>(["items", ...Object.values(TOP_FIELD_PATHS)]);
  form.items.forEach((_, index) => {
    paths.add(itemPath(index));
    for (const name of ["description", "quantity", "unit", "notes"] as const) paths.add(itemPath(index, name));
  });
  return paths;
}

/** Server `validation_error` field paths look like `draft.items[0].quantity`; findings use `items[0].quantity`. */
export function fieldErrorPathToFindingPath(path: string): string {
  if (path === "draft") return "";
  return path.startsWith("draft.") ? path.slice("draft.".length) : path;
}
