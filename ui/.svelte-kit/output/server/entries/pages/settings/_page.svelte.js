import { b as store_get, e as ensure_array_like, c as escape_html, a as attr_class, u as unsubscribe_stores } from "../../../chunks/index2.js";
import { a as accounts } from "../../../chunks/stores.js";
function _page($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    var $$store_subs;
    function formatDate(dateStr) {
      return new Date(dateStr).toLocaleString();
    }
    $$renderer2.push(`<div class="h-full overflow-y-auto p-6 space-y-6"><h1 class="text-xl font-semibold text-text-primary">Settings</h1> <section class="bg-surface rounded-lg p-4"><h2 class="text-sm font-medium text-text-primary mb-3">System Health</h2> `);
    {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<p class="text-xs text-text-muted">Unable to reach API</p>`);
    }
    $$renderer2.push(`<!--]--></section> <section class="bg-surface rounded-lg p-4"><h2 class="text-sm font-medium text-text-primary mb-3">Accounts</h2> `);
    if (store_get($$store_subs ??= {}, "$accounts", accounts).length === 0) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<p class="text-xs text-text-muted">No accounts configured</p>`);
    } else {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<div class="space-y-3"><!--[-->`);
      const each_array_1 = ensure_array_like(store_get($$store_subs ??= {}, "$accounts", accounts));
      for (let $$index_1 = 0, $$length = each_array_1.length; $$index_1 < $$length; $$index_1++) {
        let acct = each_array_1[$$index_1];
        $$renderer2.push(`<div class="bg-surface-dark rounded-lg p-3 space-y-2"><div class="flex items-center justify-between"><span class="text-sm font-medium text-text-primary">${escape_html(acct.name)}</span> <span${attr_class(`text-[10px] px-1.5 py-0.5 rounded-full ${acct.is_active ? "bg-ham/15 text-ham" : "bg-text-muted/15 text-text-muted"}`)}>${escape_html(acct.is_active ? "Active" : "Inactive")}</span></div> <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs"><div><span class="text-text-muted">IMAP:</span> <span class="text-text-secondary">${escape_html(acct.imap_host)}:${escape_html(acct.imap_port)}</span></div> <div><span class="text-text-muted">User:</span> <span class="text-text-secondary">${escape_html(acct.imap_user)}</span></div> `);
        if (acct.smtp_host) {
          $$renderer2.push("<!--[-->");
          $$renderer2.push(`<div><span class="text-text-muted">SMTP:</span> <span class="text-text-secondary">${escape_html(acct.smtp_host)}:${escape_html(acct.smtp_port ?? 587)}</span></div>`);
        } else {
          $$renderer2.push("<!--[!-->");
        }
        $$renderer2.push(`<!--]--> <div><span class="text-text-muted">Created:</span> <span class="text-text-secondary">${escape_html(formatDate(acct.created_at))}</span></div></div></div>`);
      }
      $$renderer2.push(`<!--]--></div>`);
    }
    $$renderer2.push(`<!--]--></section></div>`);
    if ($$store_subs) unsubscribe_stores($$store_subs);
  });
}
export {
  _page as default
};
