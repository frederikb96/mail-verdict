

export const index = 5;
let component_cache;
export const component = async () => component_cache ??= (await import('../entries/pages/mail/_id_/_page.svelte.js')).default;
export const universal = {
  "ssr": false,
  "load": null
};
export const universal_id = "src/routes/mail/[id]/+page.ts";
export const imports = ["_app/immutable/nodes/5.C3TIE3LT.js","_app/immutable/chunks/D_wQV65w.js","_app/immutable/chunks/DUb7JGSp.js","_app/immutable/chunks/BWpHI5Lr.js","_app/immutable/chunks/CdG4GHaw.js","_app/immutable/chunks/-88LOKdW.js","_app/immutable/chunks/CtEP0ehi.js","_app/immutable/chunks/Cc4RqetX.js","_app/immutable/chunks/C8X5x0wB.js","_app/immutable/chunks/Ci851atE.js","_app/immutable/chunks/Bc1JcgNp.js","_app/immutable/chunks/Doxof9Wn.js","_app/immutable/chunks/CqlAt8EY.js"];
export const stylesheets = [];
export const fonts = [];
