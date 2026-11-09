import { test } from "node:test";
import assert from "node:assert/strict";
import {
  attachKeyboardSafeViewport,
  VIEWPORT_HEIGHT_VARIABLE,
  type ViewportLike,
  type WindowLike,
} from "./keyboard-safe-viewport.ts";

/** Safari's keyboard pan as measured in the iOS simulator: the document scrolls by the pan and the
 *  visual viewport's `offsetTop` follows; `scrollTo(0, 0)` undoes both. */
function fakes(scale = 1) {
  const listeners = new Map<string, Set<() => void>>();
  const on = (key: string) => (type: string, fn: () => void) => {
    if (!listeners.has(`${key}:${type}`)) listeners.set(`${key}:${type}`, new Set());
    listeners.get(`${key}:${type}`)!.add(fn);
  };
  const off = (key: string) => (type: string, fn: () => void) => {
    listeners.get(`${key}:${type}`)?.delete(fn);
  };
  const fire = (key: string, type: string) => listeners.get(`${key}:${type}`)?.forEach((fn) => fn());

  const viewport = {
    height: 800,
    offsetTop: 0,
    scale,
    addEventListener: on("vv"),
    removeEventListener: off("vv"),
  } as ViewportLike;
  const scrolls: Array<[number, number]> = [];
  const win = {
    visualViewport: viewport,
    scrollY: 0,
    scrollTo(x: number, y: number) {
      scrolls.push([x, y]);
      win.scrollY = 0;
      viewport.offsetTop = 0;
    },
    addEventListener: on("win"),
    removeEventListener: off("win"),
  } as WindowLike & { scrollY: number };
  const props = new Map<string, string>();
  const root = {
    style: {
      setProperty: (k: string, v: string) => props.set(k, v),
      removeProperty: (k: string) => props.delete(k),
    },
  } as unknown as HTMLElement;
  const pan = (by: number, height: number) => {
    viewport.height = height;
    viewport.offsetTop = by;
    win.scrollY = by;
    fire("vv", "resize");
    fire("vv", "scroll");
  };
  return { viewport, win, root, props, scrolls, pan };
}

test("the variable follows the visual viewport's height", () => {
  const f = fakes();
  attachKeyboardSafeViewport(f.win, f.root);
  assert.equal(f.props.get(VIEWPORT_HEIGHT_VARIABLE), "800px");
  f.pan(0, 463);
  assert.equal(f.props.get(VIEWPORT_HEIGHT_VARIABLE), "463px");
});

test("a pan for the keyboard is undone by scrolling the document back to the top", () => {
  const f = fakes();
  attachKeyboardSafeViewport(f.win, f.root);
  f.pan(337, 463);
  assert.deepEqual(f.scrolls, [[0, 0]]);
  assert.equal(f.win.scrollY, 0);
});

test("a pinch-zoom's pan is left alone", () => {
  const f = fakes(2);
  attachKeyboardSafeViewport(f.win, f.root);
  f.pan(120, 400);
  assert.deepEqual(f.scrolls, []);
});

test("detaching stops listening and clears the variable", () => {
  const f = fakes();
  const detach = attachKeyboardSafeViewport(f.win, f.root);
  detach();
  assert.equal(f.props.has(VIEWPORT_HEIGHT_VARIABLE), false);
  f.pan(337, 463);
  assert.deepEqual(f.scrolls, []);
  assert.equal(f.props.has(VIEWPORT_HEIGHT_VARIABLE), false);
});

test("a browser without visualViewport is left alone", () => {
  const f = fakes();
  f.win.visualViewport = undefined;
  attachKeyboardSafeViewport(f.win, f.root)();
  assert.equal(f.props.size, 0);
});
