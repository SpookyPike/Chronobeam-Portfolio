import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs without globals, so React Testing Library's auto-cleanup is not
// registered; without this, renders accumulate across tests and duplicate
// queries (e.g. two "Month" calendars) break later tests.
afterEach(() => cleanup());

// jsdom does not implement scrollTo; TanStack Router calls it on navigation.
window.scrollTo = () => {};
// …nor scrollIntoView, which the month navigation uses to keep the current
// section inside its phone-width scroller.
Element.prototype.scrollIntoView = () => {};
