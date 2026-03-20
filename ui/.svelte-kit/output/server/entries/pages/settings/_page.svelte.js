import { e as ensure_array_like, a as attr_class, c as escape_html } from "../../../chunks/index2.js";
function _page($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    const CATEGORIES = ["ai", "spam", "sync", "retry"];
    let activeTab = "ai";
    $$renderer2.push(`<div class="h-full overflow-y-auto p-6 space-y-6"><h1 class="text-xl font-semibold text-text-primary">Settings</h1> `);
    {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> `);
    {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> <div class="flex gap-1 border-b border-border"><!--[-->`);
    const each_array = ensure_array_like(CATEGORIES);
    for (let $$index = 0, $$length = each_array.length; $$index < $$length; $$index++) {
      let cat = each_array[$$index];
      $$renderer2.push(`<button${attr_class("px-4 py-2 text-xs font-medium transition-colors border-b-2 -mb-px", void 0, {
        "border-accent": activeTab === cat,
        "text-text-primary": activeTab === cat,
        "border-transparent": activeTab !== cat,
        "text-text-muted": activeTab !== cat
      })}>${escape_html(cat.toUpperCase())}</button>`);
    }
    $$renderer2.push(`<!--]--></div> `);
    {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading...</div>`);
    }
    $$renderer2.push(`<!--]--></div>`);
  });
}
export {
  _page as default
};
