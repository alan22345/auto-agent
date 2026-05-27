// Ambient declaration for cytoscape-fcose. The package ships no
// declaration file. The extension's API is "call ``cytoscape.use``
// once with the default export to register, then reference the layout
// by ``name: 'fcose'``". See
// https://github.com/iVis-at-Bilkent/cytoscape.js-fcose.

declare module 'cytoscape-fcose' {
  const fcose: unknown;
  export default fcose;
}
