import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {TokenDialogHost} from "../src/components/TokenDialog";
import {getToken, setToken, useAuthStatus} from "../src/lib/auth";
import {getMsgpackIfChanged, request} from "../src/lib/api";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
let authenticated: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  setToken(null);
  useAuthStatus.setState({required: false});
  authenticated = vi.fn();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function render() {
  act(() => root.render(<TokenDialogHost onAuthenticated={authenticated} />));
}

function unauthorized() {
  return new Response("{}", {status: 401, headers: {"WWW-Authenticate": "Bearer"}});
}

async function rejectAuthentication() {
  await act(async () => {
    await expect(request("api/health")).rejects.toMatchObject({status: 401});
  });
}

function enter(value: string) {
  const input = host.querySelector("input")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", {bubbles: true}));
  });
}

async function submit() {
  await act(async () => {
    host.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
  });
}

describe("console token sign in", () => {
  it("does not prompt when the server accepts requests without a token", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}")));
    render();
    await act(async () => { await request("api/health"); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(getToken()).toBeNull();
  });

  it.each([401, 403, 500, 503])("does not treat HTTP %s without a bearer challenge as a request for the console token", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", {status})));
    render();
    await act(async () => { await expect(request("api/health")).rejects.toMatchObject({status}); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("does not prompt just because the server is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    render();
    await act(async () => { await expect(request("api/health")).rejects.toMatchObject({status: 0}); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("opens a focused password field even if the rejection preceded rendering", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(unauthorized()));
    await rejectAuthentication();
    render();
    const input = host.querySelector("input")!;
    expect(host.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe("Sign in to ADT4");
    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("current-password");
    expect(document.activeElement).toBe(input);
    expect(host.querySelector("button")!.disabled).toBe(true);
  });

  it("retries an incorrect token, saves only a valid token, then reconnects", async () => {
    setToken("expired-token");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(unauthorized())
      .mockResolvedValueOnce(unauthorized())
      .mockResolvedValueOnce(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    render();
    await rejectAuthentication();
    enter("wrong-token");
    await submit();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Incorrect token");
    expect(getToken()).toBe("expired-token");
    expect(authenticated).not.toHaveBeenCalled();
    enter("correct-token");
    await submit();
    expect(getToken()).toBe("correct-token");
    expect(localStorage.getItem("adt4_console_token")).toBe("correct-token");
    expect(authenticated).toHaveBeenCalledOnce();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    const [url, init] = fetchMock.mock.calls[2];
    expect(url).toBe("api/health");
    expect(init.headers.Authorization).toBe("Bearer correct-token");
    expect(window.location.search).not.toContain("correct-token");
  });

  it("keeps the dialog available for retry if validation loses its connection", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(unauthorized())
      .mockRejectedValueOnce(new TypeError("offline")));
    render();
    await rejectAuthentication();
    enter("candidate");
    await submit();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("server unreachable");
    expect(host.querySelector("button")!.disabled).toBe(false);
    expect(getToken()).toBeNull();
    expect(authenticated).not.toHaveBeenCalled();
  });

  it("uses a valid saved token without prompting", async () => {
    setToken("saved-token");
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    render();
    await act(async () => { await request("api/health"); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(fetchMock.mock.calls[0][1].headers.get("Authorization")).toBe("Bearer saved-token");
  });

  it("prompts when a polling request discovers the server token has changed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(unauthorized()));
    render();
    await act(async () => {
      await expect(getMsgpackIfChanged("api/graph/prior", "version")).rejects.toMatchObject({status: 401});
    });
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
  });
});
