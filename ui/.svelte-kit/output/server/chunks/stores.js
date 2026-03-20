import { d as derived, w as writable } from "./index.js";
const accounts = writable([]);
const currentAccount = writable(null);
const folders = writable([]);
const selectedFolder = writable(null);
const mails = writable([]);
const selectedMail = writable(null);
const sidebarCollapsed = writable(false);
derived(
  folders,
  ($folders) => $folders.find((f) => f.special_use === "inbox" || f.imap_name === "INBOX") ?? null
);
const foldersBySpecialUse = derived(folders, ($folders) => {
  const special = [];
  const regular = [];
  for (const f of $folders) {
    if (f.special_use) {
      special.push(f);
    } else {
      regular.push(f);
    }
  }
  return { special, regular };
});
export {
  accounts as a,
  foldersBySpecialUse as b,
  currentAccount as c,
  sidebarCollapsed as d,
  selectedMail as e,
  folders as f,
  mails as m,
  selectedFolder as s
};
