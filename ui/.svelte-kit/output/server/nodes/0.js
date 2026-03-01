

export const index = 0;
let component_cache;
export const component = async () => component_cache ??= (await import('../entries/pages/_layout.svelte.js')).default;
export const universal = {
  "ssr": false,
  "load": null
};
export const universal_id = "src/routes/+layout.ts";
export const imports = ["_app/immutable/nodes/0.BJIRZCAY.js","_app/immutable/chunks/CjBSVnT5.js","_app/immutable/chunks/FbMuewAP.js","_app/immutable/chunks/DaaBLr1T.js","_app/immutable/chunks/BUR4cSnH.js","_app/immutable/chunks/F5i-DLzj.js","_app/immutable/chunks/tBozt265.js","_app/immutable/chunks/yAegmjCS.js","_app/immutable/chunks/CuaR_AFx.js","_app/immutable/chunks/BsK_FuP7.js"];
export const stylesheets = ["_app/immutable/assets/0.WR2m3k0V.css"];
export const fonts = [];
