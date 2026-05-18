// Ambient declaration for cytoscape-expand-collapse v4 (no
// @types/cytoscape-expand-collapse on npm). The extension's API is
// "call the default export once with the cytoscape constructor to
// register, then ``cy.expandCollapse(opts)`` returns an API object" —
// see https://github.com/iVis-at-Bilkent/cytoscape.js-expand-collapse.

declare module 'cytoscape-expand-collapse' {
  const register: (cy: unknown) => void;
  export default register;
}
