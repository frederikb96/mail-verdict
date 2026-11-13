import { test } from "node:test";
import assert from "node:assert/strict";
import {
  groupState,
  isSystemFolder,
  listedFolders,
  toggleFolder,
  toggleGroup,
} from "./folder-picker.ts";
import type { FolderResponse } from "../types/api.ts";

function folder(id: string, name: string, extra: Partial<FolderResponse> = {}): FolderResponse {
  return {
    id,
    account_id: "a",
    imap_name: name,
    display_name: null,
    special_use: null,
    is_visible: true,
    kind: "imap",
    ...extra,
  } as FolderResponse;
}

const folders = [
  folder("sent", "Sent", { special_use: "sent" }),
  folder("promo", "Promo"),
  folder("inbox", "INBOX", { special_use: "inbox" }),
  folder("cal", "Kalender", { is_visible: false }),
];

const none = new Set<string>();

test("drafts, sent, trash, junk and hidden folders are system folders", () => {
  assert.equal(isSystemFolder(folders[0]), true);
  assert.equal(isSystemFolder(folders[3]), true);
  assert.equal(isSystemFolder(folders[1]), false);
  assert.equal(isSystemFolder(folders[2]), false);
});

test("hiding system folders leaves the rest, special-use first", () => {
  const ids = listedFolders(folders, "Posteo", { hideSystem: true, query: "", selected: none }).map((f) => f.id);
  assert.deepEqual(ids, ["inbox", "promo"]);
});

test("a ticked system folder stays listed", () => {
  const ids = listedFolders(folders, "Posteo", {
    hideSystem: true,
    query: "",
    selected: new Set(["sent"]),
  }).map((f) => f.id);
  assert.deepEqual(ids, ["inbox", "sent", "promo"]);
});

test("a query matches the folder name, or every folder of a matching account", () => {
  const byName = listedFolders(folders, "Posteo", { hideSystem: false, query: "prom", selected: none });
  assert.deepEqual(byName.map((f) => f.id), ["promo"]);
  const byAccount = listedFolders(folders, "Posteo", { hideSystem: false, query: "poste", selected: none });
  assert.equal(byAccount.length, 4);
});

test("the account box is none, some or all of what it lists", () => {
  assert.equal(groupState(["a", "b"], new Set()), "none");
  assert.equal(groupState(["a", "b"], new Set(["a"])), "some");
  assert.equal(groupState(["a", "b"], new Set(["a", "b", "z"])), "all");
});

test("clicking an account ticks what it lists, and clears it once all are ticked", () => {
  assert.deepEqual(toggleGroup(["z"], ["a", "b"]), ["z", "a", "b"]);
  assert.deepEqual(toggleGroup(["z", "a"], ["a", "b"]), ["z", "a", "b"]);
  assert.deepEqual(toggleGroup(["z", "a", "b"], ["a", "b"]), ["z"]);
});

test("toggling one folder is idempotent", () => {
  assert.deepEqual(toggleFolder(["a"], "a", true), ["a"]);
  assert.deepEqual(toggleFolder(["a"], "b", false), ["a"]);
  assert.deepEqual(toggleFolder(["a"], "a", false), []);
});
