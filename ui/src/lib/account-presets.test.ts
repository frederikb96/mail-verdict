import { test } from "node:test";
import assert from "node:assert/strict";
import { presetById, presetConnection } from "./account-presets.ts";

test("Gmail signs in to both servers with the full address and one app password", () => {
  const request = presetConnection(presetById("gmail"), " me@gmail.com ", "abcd efgh");

  assert.deepEqual(request, {
    imap_host: "imap.gmail.com",
    imap_port: 993,
    imap_user: "me@gmail.com",
    imap_password: "abcd efgh",
    smtp_host: "smtp.gmail.com",
    smtp_port: 465,
    smtp_user: "me@gmail.com",
    smtp_password: "abcd efgh",
  });
});

test("iCloud signs in to IMAP with the part before the @ and to SMTP with the full address", () => {
  const request = presetConnection(presetById("icloud"), "me@icloud.com", "pw");

  assert.equal(request.imap_user, "me");
  assert.equal(request.smtp_user, "me@icloud.com");
  assert.equal(request.imap_host, "imap.mail.me.com");
  assert.equal(request.smtp_port, 587);
});
