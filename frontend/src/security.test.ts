import { describe, expect, it } from "vitest";

// Static guards over the APPLICATION source (test files and test helpers are excluded). They fail when someone adds a
// pattern the security requirements of this slice forbid.
const sources = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx", "!./test/**"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const FORBIDDEN: Array<[string, RegExp]> = [
  ["dangerouslySetInnerHTML (user content must be rendered as text)", /dangerouslySetInnerHTML/],
  ["innerHTML / outerHTML / insertAdjacentHTML", /\b(innerHTML|outerHTML|insertAdjacentHTML)\b/],
  ["localStorage (no credentials or tokens in browser storage)", /\blocalStorage\b/],
  ["sessionStorage (no credentials or tokens in browser storage)", /\bsessionStorage\b/],
  ["indexedDB", /\bindexedDB\b/],
  ["document.cookie (the CSRF token comes from the API body; cookies are HttpOnly)", /document\.cookie/],
  ["console output (no sensitive data in logs)", /\bconsole\s*\./],
  ["eval / new Function", /\beval\s*\(|new\s+Function\s*\(/],
  ["a hard-coded absolute URL (all calls are same-origin)", /["'`]https?:\/\//],
  ["JWT / bearer tokens (session cookies only)", /\bAuthorization\b|\bBearer\b|\bjwt\b/i],
];

describe("application source", () => {
  it("is being scanned (guards against an empty glob)", () => {
    const names = Object.keys(sources);
    expect(names.length).toBeGreaterThan(15);
    expect(names).toContain("./api/http.ts");
    expect(names.some((n) => n.includes("test"))).toBe(false);
  });

  it.each(FORBIDDEN)("contains no %s", (_label, pattern) => {
    const offenders = Object.entries(sources)
      .filter(([, text]) => pattern.test(stripComments(text)))
      .map(([name]) => name);
    expect(offenders).toEqual([]);
  });

  it("only ever calls fetch from the HTTP client", () => {
    const offenders = Object.entries(sources)
      .filter(([name, text]) => name !== "./api/http.ts" && /\bfetch\s*\(/.test(stripComments(text)))
      .map(([name]) => name);
    expect(offenders).toEqual([]);
  });

  it("never converts a quantity to a JavaScript number", () => {
    const offenders = Object.entries(sources)
      .filter(([, text]) => /(Number|parseFloat|parseInt)\s*\([^)]*quantity|quantity[^;\n]*\b(Number|parseFloat|parseInt)\s*\(|\+\s*\w*\.?quantity\b/.test(stripComments(text)))
      .map(([name]) => name);
    expect(offenders).toEqual([]);
  });
});

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}
