/**
 * Optional presentation-work cruise. The canonical config owns all boundary
 * rules; this adds only the two demo Playwright configs as known entry points.
 *
 * Run: `pnpm depcruise:development`.
 */
const config = require("./.dependency-cruiser.cjs");
const noOrphans = config.forbidden.find((rule) => rule.name === "no-orphans");
const exclusions = noOrphans.from.pathNot;
noOrphans.from.pathNot = [
  ...(Array.isArray(exclusions) ? exclusions : [exclusions]),
  "^demo/playwright\\..*\\.config\\.ts$",
];

module.exports = config;
