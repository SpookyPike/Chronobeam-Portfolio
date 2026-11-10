/**
 * dependency-cruiser — architecture rules for the Chronobeam frontend.
 *
 * Run: `pnpm depcruise` (or `just arch` from the repository root).
 *
 * Scope of the canonical cruise is defined by the CLI arguments in the package
 * script, not here: `src e2e` plus the two root config files. `node_modules` is
 * excluded and not followed, so third-party internals never appear in results.
 *
 * `options.tsConfig` is deliberately NOT set: this project has no `paths`
 * aliases, so enhanced-resolve's extension probing is sufficient. It also keeps
 * the rule set independent of the `typescript` compiler API — see the note in
 * `frontend/pnpm-workspace.yaml` about TypeScript 7.
 *
 * @type {import('dependency-cruiser').IConfiguration}
 */
module.exports = {
  forbidden: [
    /* ------------------------------------------------------------------ *
     * Cycles
     * ------------------------------------------------------------------ */
    {
      name: "no-circular",
      severity: "error",
      comment:
        "A circular dependency means the modules cannot be reasoned about or " +
        "tested independently. Break it by extracting the shared part into a " +
        "third module (usually under src/lib/).",
      from: {},
      to: { circular: true },
    },

    /* ------------------------------------------------------------------ *
     * Resolution
     * ------------------------------------------------------------------ */
    {
      name: "not-to-unresolvable",
      severity: "error",
      comment:
        "The import cannot be resolved to a file or package. Either the path " +
        "is wrong or the package is missing from package.json.",
      from: {},
      to: {
        couldNotResolve: true,
        // dependency-cruiser's `swc` parser reads the bare word `import`
        // inside JSX text as an import statement and emits a placeholder
        // module named "" (background in frontend/pnpm-workspace.yaml). That
        // is a parser artifact, not a dependency. This matches only that one
        // placeholder, so every genuinely broken import — which always has a
        // real specifier — is still reported.
        pathNot: "^✖$",
      },
    },

    /* ------------------------------------------------------------------ *
     * Dead code
     * ------------------------------------------------------------------ */
    {
      name: "no-orphans",
      severity: "error",
      comment:
        "A module with no importers and no imports of its own is unreachable. " +
        "Delete it, or wire it up as an entry point.",
      from: {
        orphan: true,
        // Only modules that can be reached from somewhere matter. Files whose
        // whole purpose is to be an entry point legitimately have no importer:
        // index.html loads main.tsx, the Playwright suites are collected by
        // glob, and the two root configs are passed as cruise inputs. Test files and
        // the Vitest setup are likewise loaded by their runners, and Playwright
        // specs import nothing but @playwright/test, which is not followed.
        pathNot: [
          "(^|/)node_modules/",
          "^src/main\\.tsx$",
          "^src/test/",
          "\\.(test|spec)\\.tsx?$",
          "^vite\\.config\\.ts$",
          "^playwright\\.config\\.ts$",
          "\\.(css|svg|png|json)$",
        ],
      },
      to: {},
    },

    /* ------------------------------------------------------------------ *
     * Layering
     * ------------------------------------------------------------------ */
    {
      name: "lib-is-a-leaf",
      severity: "error",
      comment:
        "src/lib/** is the shared foundation: it must stay importable by " +
        "anything without dragging in the page layer. Depending on a page, " +
        "the router, the shell or the entry point inverts the layering.",
      from: { path: "^src/lib/" },
      to: { path: "^src/(pages/|router\\.tsx$|shell\\.tsx$|main\\.tsx$)" },
    },
    {
      name: "app-not-to-test-harness",
      severity: "error",
      comment:
        "Shipped application code must not import the demo or e2e harness. " +
        "Those suites drive the running app through the browser; sharing " +
        "modules with them would ship test scaffolding to production.",
      from: { path: "^src/" },
      to: { path: "^(demo|e2e)/" },
    },
    {
      name: "harness-not-to-app-internals",
      severity: "error",
      comment:
        "The Playwright suites exercise the deployed application over HTTP " +
        "and must not reach into src/** — that would turn an end-to-end test " +
        "into a unit test and hide real integration breakage. Component-level " +
        "coverage belongs in the Vitest suite.",
      from: { path: "^(demo|e2e)/" },
      to: { path: "^src/" },
    },
    {
      name: "production-not-to-tests",
      severity: "error",
      comment:
        "A non-test module importing a *.test.* module pulls test code into " +
        "the production bundle.",
      from: { path: "^src/", pathNot: "\\.test\\.tsx?$" },
      to: { path: "\\.test\\.tsx?$" },
    },

    /* ------------------------------------------------------------------ *
     * Package hygiene
     * ------------------------------------------------------------------ */
    {
      name: "no-dev-dep-in-shipped-code",
      severity: "error",
      comment:
        "Shipped source may only import runtime `dependencies`. A " +
        "devDependency import would break the production build. Test files " +
        "and the Vitest setup file are the sanctioned exceptions.",
      from: { path: "^src/", pathNot: ["\\.test\\.tsx?$", "^src/test/"] },
      to: { dependencyTypes: ["npm-dev"] },
    },
    {
      name: "no-duplicate-dep-types",
      severity: "error",
      comment:
        "A package listed in both `dependencies` and `devDependencies` is " +
        "ambiguous: the two entries can drift apart.",
      from: {},
      to: { moreThanOneDependencyType: true, dependencyTypesNot: ["type-only"] },
    },
  ],

  options: {
    // Do not traverse *into* third-party packages: their internals are noise
    // and their cycles are not ours to fix. The direct edge to the package is
    // still recorded, which is what `no-dev-dep-in-shipped-code` and
    // `no-duplicate-dep-types` need.
    //
    // Deliberately NOT `exclude: node_modules` as well: `exclude` drops the
    // module (and therefore the edge) from the result entirely, which silently
    // makes every rule that targets a package unreachable.
    doNotFollow: { path: "(^|/)node_modules/" },

    // dependency-cruiser defaults `exportsFields` to [] for backwards
    // compatibility with enhanced-resolve 4. Packages that declare only an
    // `exports` map and no `main` (e.g. @vitejs/plugin-react, imported by
    // vite.config.ts) are unresolvable under that default. Honouring
    // `exports` with the conditions Node and the bundler actually use is what
    // the runtime does, so resolution stays accurate instead of loosened.
    enhancedResolveOptions: {
      exportsFields: ["exports"],
      mainFields: ["module", "main"],
      conditionNames: ["import", "require", "node", "default"],
    },

    // Uncomment to see the full module graph in a browser instead of the CLI:
    // reporterOptions: { archi: { outputTo: "artifacts/dependency-graph.html" } },
  },
};
