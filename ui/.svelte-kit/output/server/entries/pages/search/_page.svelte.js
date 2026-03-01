import "clsx";
import "@sveltejs/kit/internal";
import "../../../chunks/exports.js";
import "../../../chunks/utils.js";
import "@sveltejs/kit/internal/server";
import "../../../chunks/root.js";
import "../../../chunks/state.svelte.js";
import "../../../chunks/stores.js";
function _page($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    $$renderer2.push(`<div class="h-full overflow-y-auto p-6 space-y-4"><h1 class="text-xl font-semibold text-text-primary">Search</h1> `);
    {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<div class="py-12 text-center text-text-muted text-sm">Enter a search query</div>`);
    }
    $$renderer2.push(`<!--]--></div>`);
  });
}
export {
  _page as default
};
