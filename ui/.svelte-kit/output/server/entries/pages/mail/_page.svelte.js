import { b as store_get, u as unsubscribe_stores } from "../../../chunks/index2.js";
import { M as MailList } from "../../../chunks/MailList.js";
import { e as selectedMail } from "../../../chunks/stores.js";
function _page($$renderer) {
  var $$store_subs;
  $$renderer.push(`<div class="h-full flex"><div class="w-96 flex-shrink-0 border-r border-border">`);
  MailList($$renderer);
  $$renderer.push(`<!----></div> <div class="flex-1 flex items-center justify-center">`);
  if (!store_get($$store_subs ??= {}, "$selectedMail", selectedMail)) {
    $$renderer.push("<!--[-->");
    $$renderer.push(`<div class="text-text-muted text-sm">Select a message to read</div>`);
  } else {
    $$renderer.push("<!--[!-->");
  }
  $$renderer.push(`<!--]--></div></div>`);
  if ($$store_subs) unsubscribe_stores($$store_subs);
}
export {
  _page as default
};
