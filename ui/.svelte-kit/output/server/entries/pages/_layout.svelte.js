import { s as ssr_context, a as attr_class, b as store_get, e as ensure_array_like, c as escape_html, d as attr, u as unsubscribe_stores, f as store_set } from "../../chunks/index2.js";
import { f as folders, a as accounts, b as foldersBySpecialUse, c as currentAccount, s as selectedFolder, d as sidebarCollapsed } from "../../chunks/stores.js";
import { a as api } from "../../chunks/api.js";
import "@sveltejs/kit/internal";
import "../../chunks/exports.js";
import "../../chunks/utils.js";
import "@sveltejs/kit/internal/server";
import "../../chunks/root.js";
import "../../chunks/state.svelte.js";
import "clsx";
function onDestroy(fn) {
  /** @type {SSRContext} */
  ssr_context.r.on_destroy(fn);
}
function Sidebar($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    var $$store_subs;
    const FOLDER_ICONS = {
      inbox: "📥",
      sent: "📤",
      drafts: "📝",
      trash: "🗑",
      junk: "⚠",
      archive: "📦"
    };
    function folderIcon(specialUse) {
      if (specialUse && FOLDER_ICONS[specialUse]) return FOLDER_ICONS[specialUse];
      return "📁";
    }
    function folderDisplayName(folder) {
      return folder.display_name ?? folder.imap_name;
    }
    async function selectAccount(account) {
      if (!account) return;
      store_set(currentAccount, account);
      store_set(selectedFolder, null);
      try {
        store_set(folders, await api.folders.list(account.id));
      } catch {
        store_set(folders, []);
      }
    }
    $$renderer2.push(`<aside${attr_class("h-full flex flex-col bg-surface border-r border-border transition-all duration-200", void 0, {
      "w-64": !store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed),
      "w-14": store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed)
    })}><div class="flex items-center justify-between p-3 border-b border-border">`);
    if (!store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed)) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<span class="text-sm font-semibold text-text-primary tracking-wide">MailVerdict</span>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> <button class="p-1 rounded hover:bg-surface-light text-text-muted hover:text-text-primary transition-colors" aria-label="Toggle sidebar">`);
    if (store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed)) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7"></path></svg>`);
    } else {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7"></path></svg>`);
    }
    $$renderer2.push(`<!--]--></button></div> `);
    if (!store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed) && store_get($$store_subs ??= {}, "$accounts", accounts).length > 0) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="p-2 border-b border-border">`);
      $$renderer2.select(
        {
          class: "w-full bg-surface-dark text-text-primary text-xs rounded px-2 py-1.5 border border-border focus:border-accent focus:outline-none",
          onchange: (e) => {
            const target = e.target;
            const acct = store_get($$store_subs ??= {}, "$accounts", accounts).find((a) => a.id === target.value);
            if (acct) selectAccount(acct);
          },
          value: store_get($$store_subs ??= {}, "$currentAccount", currentAccount)?.id ?? ""
        },
        ($$renderer3) => {
          $$renderer3.push(`<!--[-->`);
          const each_array = ensure_array_like(store_get($$store_subs ??= {}, "$accounts", accounts));
          for (let $$index = 0, $$length = each_array.length; $$index < $$length; $$index++) {
            let acct = each_array[$$index];
            $$renderer3.option({ value: acct.id }, ($$renderer4) => {
              $$renderer4.push(`${escape_html(acct.name)}`);
            });
          }
          $$renderer3.push(`<!--]-->`);
        }
      );
      $$renderer2.push(`</div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> <nav class="flex-1 overflow-y-auto py-1">`);
    if (!store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed)) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<!--[-->`);
      const each_array_1 = ensure_array_like(store_get($$store_subs ??= {}, "$foldersBySpecialUse", foldersBySpecialUse).special);
      for (let $$index_1 = 0, $$length = each_array_1.length; $$index_1 < $$length; $$index_1++) {
        let folder = each_array_1[$$index_1];
        $$renderer2.push(`<button${attr_class("w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors hover:bg-surface-light", void 0, {
          "bg-surface-light": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
          "text-accent": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
          "text-text-secondary": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id !== folder.id
        })}><span class="text-xs">${escape_html(folderIcon(folder.special_use))}</span> <span class="truncate">${escape_html(folderDisplayName(folder))}</span></button>`);
      }
      $$renderer2.push(`<!--]--> `);
      if (store_get($$store_subs ??= {}, "$foldersBySpecialUse", foldersBySpecialUse).regular.length > 0) {
        $$renderer2.push("<!--[-->");
        $$renderer2.push(`<div class="px-3 pt-3 pb-1"><span class="text-[10px] uppercase tracking-wider text-text-muted font-medium">Folders</span></div> <!--[-->`);
        const each_array_2 = ensure_array_like(store_get($$store_subs ??= {}, "$foldersBySpecialUse", foldersBySpecialUse).regular);
        for (let $$index_2 = 0, $$length = each_array_2.length; $$index_2 < $$length; $$index_2++) {
          let folder = each_array_2[$$index_2];
          $$renderer2.push(`<button${attr_class("w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors hover:bg-surface-light", void 0, {
            "bg-surface-light": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
            "text-accent": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
            "text-text-secondary": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id !== folder.id
          })}><span class="text-xs">${escape_html(folderIcon(folder.special_use))}</span> <span class="truncate">${escape_html(folderDisplayName(folder))}</span></button>`);
        }
        $$renderer2.push(`<!--]-->`);
      } else {
        $$renderer2.push("<!--[!-->");
      }
      $$renderer2.push(`<!--]-->`);
    } else {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<!--[-->`);
      const each_array_3 = ensure_array_like(store_get($$store_subs ??= {}, "$foldersBySpecialUse", foldersBySpecialUse).special);
      for (let $$index_3 = 0, $$length = each_array_3.length; $$index_3 < $$length; $$index_3++) {
        let folder = each_array_3[$$index_3];
        $$renderer2.push(`<button${attr_class("w-full flex items-center justify-center py-2 transition-colors hover:bg-surface-light", void 0, {
          "bg-surface-light": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
          "text-accent": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id === folder.id,
          "text-text-muted": store_get($$store_subs ??= {}, "$selectedFolder", selectedFolder)?.id !== folder.id
        })}${attr("title", folderDisplayName(folder))}><span class="text-sm">${escape_html(folderIcon(folder.special_use))}</span></button>`);
      }
      $$renderer2.push(`<!--]-->`);
    }
    $$renderer2.push(`<!--]--></nav> `);
    if (!store_get($$store_subs ??= {}, "$sidebarCollapsed", sidebarCollapsed)) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="border-t border-border p-2 space-y-0.5"><a href="/" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"></path></svg> Dashboard</a> <a href="/search" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg> Search</a> <a href="/accounts" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"></path></svg> Accounts</a> <a href="/verdicts" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg> Verdicts</a> <a href="/settings" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg> Settings</a></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--></aside>`);
    if ($$store_subs) unsubscribe_stores($$store_subs);
  });
}
function SearchBar($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    let query = "";
    let mode = "fulltext";
    $$renderer2.push(`<form class="flex items-center gap-2"><div class="relative flex-1"><svg class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg> <input type="text"${attr("value", query)} placeholder="Search by subject, sender, or content..." class="w-full pl-8 pr-3 py-1.5 text-sm bg-surface-dark border border-border rounded text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"/></div> `);
    $$renderer2.select(
      {
        value: mode,
        class: "bg-surface-dark text-text-secondary text-xs rounded px-2 py-1.5 border border-border focus:border-accent focus:outline-none"
      },
      ($$renderer3) => {
        $$renderer3.option({ value: "fulltext" }, ($$renderer4) => {
          $$renderer4.push(`Fulltext`);
        });
        $$renderer3.option({ value: "semantic" }, ($$renderer4) => {
          $$renderer4.push(`Semantic`);
        });
      }
    );
    $$renderer2.push(` <button type="submit" class="px-3 py-1.5 text-xs bg-accent hover:bg-accent-hover text-white rounded transition-colors">Search</button></form>`);
  });
}
const RECONNECT_DELAY_MS = 3e3;
const MAX_RECONNECT_DELAY_MS = 3e4;
class SSEClient {
  source = null;
  handlers = /* @__PURE__ */ new Map();
  reconnectDelay = RECONNECT_DELAY_MS;
  reconnectTimer = null;
  accountId = null;
  connect(accountId) {
    this.disconnect();
    this.accountId = accountId ?? null;
    const url = accountId ? `/api/events?account_id=${accountId}` : "/api/events";
    this.source = new EventSource(url);
    this.source.onopen = () => {
      this.reconnectDelay = RECONNECT_DELAY_MS;
    };
    this.source.onerror = () => {
      this.source?.close();
      this.source = null;
      this.scheduleReconnect();
    };
    const eventTypes = ["new_mail", "folder_change", "flags_changed", "verdict_issued", "sync_status"];
    for (const type of eventTypes) {
      this.source.addEventListener(type, (e) => {
        try {
          const data = JSON.parse(e.data);
          this.emit(type, data);
          this.emit("*", data);
        } catch {
        }
      });
    }
  }
  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  }
  on(event, handler) {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, /* @__PURE__ */ new Set());
    }
    this.handlers.get(event).add(handler);
    return () => this.handlers.get(event)?.delete(handler);
  }
  emit(event, data) {
    const set = this.handlers.get(event);
    if (set) {
      for (const handler of set) {
        handler(data);
      }
    }
  }
  scheduleReconnect() {
    this.reconnectTimer = setTimeout(() => {
      this.connect(this.accountId ?? void 0);
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
  }
}
const sse = new SSEClient();
function _layout($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    var $$store_subs;
    let { data, children } = $$props;
    onDestroy(() => {
      sse.disconnect();
    });
    $$renderer2.push(`<div class="h-screen flex overflow-hidden bg-surface-dark">`);
    Sidebar($$renderer2);
    $$renderer2.push(`<!----> <div class="flex-1 flex flex-col min-w-0"><header class="flex items-center gap-3 px-4 py-2 border-b border-border bg-surface"><div class="flex-1 max-w-md">`);
    SearchBar($$renderer2);
    $$renderer2.push(`<!----></div> `);
    if (store_get($$store_subs ??= {}, "$currentAccount", currentAccount)) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<span class="text-[11px] text-text-muted hidden md:block">${escape_html(store_get($$store_subs ??= {}, "$currentAccount", currentAccount).name)} (${escape_html(store_get($$store_subs ??= {}, "$currentAccount", currentAccount).imap_user)})</span>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--></header> <main class="flex-1 overflow-hidden">`);
    children($$renderer2);
    $$renderer2.push(`<!----></main></div></div>`);
    if ($$store_subs) unsubscribe_stores($$store_subs);
  });
}
export {
  _layout as default
};
