import { describe, expect, it } from "vitest";

import { CREDENTIALS_PATH, SECRET_SENTINEL, credentialStatus } from "../test/aiSettings";
import { apiError, installFakeApi, json } from "../test/fakeApi";
import { createAiCredential, deleteAiCredential, getAiCredentialStatus, parseCredentialStatus, replaceAiCredential } from "./aiCredentials";
import { ApiError } from "./http";

const REF = "AP_AI_CRED_CORP_LLM";

function serve(status = 200, body: unknown = credentialStatus(REF, "set", ["corp-llm"])) {
  return installFakeApi((call) => {
    if (call.path === "/api/auth/csrf/") return json(200, { csrf_token: "token-1" });
    return json(status, body);
  });
}

describe("parseCredentialStatus", () => {
  it("returns the contract's fields", () => {
    expect(parseCredentialStatus(JSON.parse(JSON.stringify(credentialStatus(REF, "unreadable", ["a", "b"]))))).toEqual(credentialStatus(REF, "unreadable", ["a", "b"]));
  });

  it("copies only listed fields, so a rogue value is dropped", () => {
    const parsed = parseCredentialStatus({ ...credentialStatus(REF, "set"), value: SECRET_SENTINEL, secret: SECRET_SENTINEL });
    expect(JSON.stringify(parsed)).not.toContain(SECRET_SENTINEL);
    expect(Object.keys(parsed).sort()).toEqual(["apply", "connections", "reference", "source", "status", "store"]);
  });

  it.each([
    ["not an object", "x"],
    ["a list", []],
    ["an unknown status", { ...credentialStatus(REF, "set"), status: "ok" }],
    ["another source", { ...credentialStatus(REF, "set"), source: "environment" }],
    ["connections not a list", { ...credentialStatus(REF, "set"), connections: "a" }],
    ["a connection that is not text", { ...credentialStatus(REF, "set"), connections: [1] }],
    ["no reference", { ...credentialStatus(REF, "set"), reference: undefined }],
  ])("refuses a malformed answer: %s", (_name, body) => {
    expect(() => parseCredentialStatus(body)).toThrow(ApiError);
  });
});

describe("the requests", () => {
  it("reads the status with a GET that sends no body and no CSRF header", async () => {
    const api = serve();
    expect((await getAiCredentialStatus(REF)).status).toBe("set");
    const get = api.calls.find((c) => c.method === "GET" && c.path.startsWith(CREDENTIALS_PATH))!;
    expect(get.path).toBe(`${CREDENTIALS_PATH}${REF}/`);
    expect(get.body).toBeUndefined();
    expect(get.headers["x-csrftoken"]).toBeUndefined();
  });

  it("creates with a POST of exactly {reference, value}", async () => {
    const api = serve(201);
    await createAiCredential(REF, SECRET_SENTINEL);
    const post = api.callsTo("POST", CREDENTIALS_PATH)[0]!;
    expect(post.body).toEqual({ reference: REF, value: SECRET_SENTINEL });
    expect(post.headers["x-csrftoken"]).toBe("token-1");
    expect(post.credentials).toBe("same-origin");
  });

  it("replaces with a PUT of exactly {value} to the reference's own URL", async () => {
    const api = serve();
    await replaceAiCredential(REF, SECRET_SENTINEL);
    const put = api.callsTo("PUT", `${CREDENTIALS_PATH}${REF}/`)[0]!;
    expect(put.body).toEqual({ value: SECRET_SENTINEL });
    expect(put.headers["x-csrftoken"]).toBe("token-1");
  });

  it("deletes with a DELETE that has no body", async () => {
    const api = serve(200, credentialStatus(REF, "not_set"));
    expect((await deleteAiCredential(REF)).status).toBe("not_set");
    const del = api.callsTo("DELETE", `${CREDENTIALS_PATH}${REF}/`)[0]!;
    expect(del.body).toBeUndefined();
    expect(del.headers["x-csrftoken"]).toBe("token-1");
  });

  it("puts a reference into the path encoded, so it cannot change the route", async () => {
    const api = serve();
    await getAiCredentialStatus("a/b?c#d");
    expect(api.calls.find((c) => c.method === "GET" && c.url.includes("a%2Fb"))).toBeDefined();
  });

  it("turns an error answer into an ApiError that holds only the contract's code, never the value sent", async () => {
    installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : apiError(409, "credential_already_exists")));
    const error = await createAiCredential(REF, SECRET_SENTINEL).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("credential_already_exists");
    expect(JSON.stringify({ message: (error as ApiError).message, details: (error as ApiError).details })).not.toContain(SECRET_SENTINEL);
  });

  it("reports an unreachable server without the value", async () => {
    installFakeApi((call) => (call.path === "/api/auth/csrf/" ? json(200, { csrf_token: "t" }) : "network-error"));
    const error = (await replaceAiCredential(REF, SECRET_SENTINEL).catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("network_error");
    expect(error.message).not.toContain(SECRET_SENTINEL);
  });

  it("refuses an answer of the wrong shape", async () => {
    serve(200, { reference: REF });
    await expect(getAiCredentialStatus(REF)).rejects.toMatchObject({ code: "unexpected_response" });
  });
});
