import "clsx";
import "../../chunks/stores.js";
function _page($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    $$renderer2.push(`<div class="h-full overflow-y-auto p-6 space-y-6"><div class="flex items-center justify-between"><h1 class="text-xl font-semibold text-text-primary">Dashboard</h1> <button class="text-xs text-accent hover:text-accent-hover transition-colors">Refresh</button></div> `);
    {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading stats...</div>`);
    }
    $$renderer2.push(`<!--]--></div>`);
  });
}
export {
  _page as default
};
