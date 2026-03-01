import { a as attr_class, c as escape_html, b as store_get, e as ensure_array_like, u as unsubscribe_stores } from "./index2.js";
import { s as selectedFolder, m as mails, e as selectedMail } from "./stores.js";
import "@sveltejs/kit/internal";
import "./exports.js";
import "./utils.js";
import "clsx";
import "@sveltejs/kit/internal/server";
import "./root.js";
import "./state.svelte.js";
function MailListItem($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    let { mail, selected } = $$props;
    function formatDate(dateStr) {
      if (!dateStr) return "";
      const d = new Date(dateStr);
      const now = /* @__PURE__ */ new Date();
      const isToday = d.toDateString() === now.toDateString();
      if (isToday) {
        return d.toLocaleTimeString(void 0, { hour: "2-digit", minute: "2-digit" });
      }
      return d.toLocaleDateString(void 0, { month: "short", day: "numeric" });
    }
    function extractName(addr) {
      if (!addr) return "(unknown)";
      const match = addr.match(/^"?([^"<]+)"?\s*</);
      if (match) return match[1].trim();
      return addr.split("@")[0];
    }
    $$renderer2.push(`<button${attr_class("w-full text-left px-3 py-2.5 border-b border-border transition-colors hover:bg-surface-light", void 0, {
      "bg-surface-light": selected,
      "border-l-2": selected,
      "border-l-accent": selected
    })}><div class="flex items-center justify-between gap-2"><span${attr_class("text-sm truncate", void 0, {
      "font-semibold": !mail.is_read,
      "text-text-primary": !mail.is_read,
      "text-text-secondary": mail.is_read
    })}>${escape_html(extractName(mail.from_addr))}</span> <span class="text-[11px] text-text-muted whitespace-nowrap flex-shrink-0">${escape_html(formatDate(mail.received_at))}</span></div> <div${attr_class("text-xs mt-0.5 truncate", void 0, {
      "text-text-primary": !mail.is_read,
      "text-text-muted": mail.is_read
    })}>${escape_html(mail.subject ?? "(no subject)")}</div> <div class="flex items-center gap-1.5 mt-1">`);
    if (mail.is_flagged) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<span class="w-1.5 h-1.5 rounded-full bg-warn" title="Flagged"></span>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> `);
    if (!mail.is_read) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<span class="w-1.5 h-1.5 rounded-full bg-accent" title="Unread"></span>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--></div></button>`);
  });
}
function MailList($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    var $$store_subs;
    let offset = 0;
    const LIMIT = 50;
    function folderDisplayName() {
      const f = store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder);
      if (!f) return "Select a folder";
      return f.display_name ?? f.imap_name;
    }
    $$renderer2.push(`<div class="h-full flex flex-col bg-surface-dark"><div class="px-3 py-2 border-b border-border flex items-center justify-between"><h2 class="text-sm font-medium text-text-primary">${escape_html(folderDisplayName())}</h2> <span class="text-[11px] text-text-muted">${escape_html(store_get($$store_subs ??= {}, "$mails", mails).length)} messages</span></div> <div class="flex-1 overflow-y-auto">`);
    if (store_get($$store_subs ??= {}, "$mails", mails).length === 0) {
      $$renderer2.push("<!--[1-->");
      $$renderer2.push(`<div class="flex items-center justify-center py-12 text-text-muted text-sm">`);
      if (store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)) {
        $$renderer2.push("<!--[-->");
        $$renderer2.push(`No messages`);
      } else {
        $$renderer2.push("<!--[!-->");
        $$renderer2.push(`Select a folder`);
      }
      $$renderer2.push(`<!--]--></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<!--[-->`);
      const each_array = ensure_array_like(store_get($$store_subs ??= {}, "$mails", mails));
      for (let $$index = 0, $$length = each_array.length; $$index < $$length; $$index++) {
        let mail = each_array[$$index];
        MailListItem($$renderer2, {
          mail,
          selected: store_get($$store_subs ??= {}, "$selectedMail", selectedMail)?.id === mail.id
        });
      }
      $$renderer2.push(`<!--]--> `);
      if (store_get($$store_subs ??= {}, "$mails", mails).length >= offset + LIMIT) {
        $$renderer2.push("<!--[-->");
        $$renderer2.push(`<button class="w-full py-3 text-xs text-accent hover:text-accent-hover transition-colors">Load more</button>`);
      } else {
        $$renderer2.push("<!--[!-->");
      }
      $$renderer2.push(`<!--]-->`);
    }
    $$renderer2.push(`<!--]--></div></div>`);
    if ($$store_subs) unsubscribe_stores($$store_subs);
  });
}
export {
  MailList as M
};
