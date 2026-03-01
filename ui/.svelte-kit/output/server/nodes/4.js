

export const index = 4;
let component_cache;
export const component = async () => component_cache ??= (await import('../entries/pages/mail/_id_/_page.svelte.js')).default;
export const universal = {
  "ssr": false,
  "load": null
};
export const universal_id = "src/routes/mail/[id]/+page.ts";
export const imports = ["_app/immutable/nodes/4.BWRCqln_.js","_app/immutable/chunks/CjBSVnT5.js","_app/immutable/chunks/FbMuewAP.js","_app/immutable/chunks/BsK_FuP7.js","_app/immutable/chunks/DaaBLr1T.js","_app/immutable/chunks/BUR4cSnH.js","_app/immutable/chunks/CrCwT2An.js","_app/immutable/chunks/yAegmjCS.js","_app/immutable/chunks/DMl8QVDf.js","_app/immutable/chunks/tBozt265.js"];
export const stylesheets = [];
export const fonts = [];
