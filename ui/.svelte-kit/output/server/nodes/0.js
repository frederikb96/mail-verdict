

export const index = 0;
let component_cache;
export const component = async () => component_cache ??= (await import('../entries/pages/_layout.svelte.js')).default;
export const universal = {
  "ssr": false,
  "load": null
};
export const universal_id = "src/routes/+layout.ts";
export const imports = ["_app/immutable/nodes/0.D3nEbj6A.js","_app/immutable/chunks/D_wQV65w.js","_app/immutable/chunks/DUb7JGSp.js","_app/immutable/chunks/-88LOKdW.js","_app/immutable/chunks/C8X5x0wB.js","_app/immutable/chunks/4Q8sSpBh.js","_app/immutable/chunks/CtEP0ehi.js","_app/immutable/chunks/CdG4GHaw.js","_app/immutable/chunks/DvpAchgh.js","_app/immutable/chunks/CqlAt8EY.js","_app/immutable/chunks/Bc1JcgNp.js","_app/immutable/chunks/ChWFqkKj.js","_app/immutable/chunks/Ci851atE.js","_app/immutable/chunks/BWpHI5Lr.js","_app/immutable/chunks/JxOXe_ju.js"];
export const stylesheets = ["_app/immutable/assets/0.C5SMFw-A.css"];
export const fonts = [];
