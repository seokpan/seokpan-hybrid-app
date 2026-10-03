import "@testing-library/jest-dom/vitest";

// JSDOM has no native modal/top-layer behavior. Browser tests verify focus and
// layout; component tests only model the open/close state of HTMLDialogElement.
HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute("open", "");
};
HTMLDialogElement.prototype.close = function () {
  this.removeAttribute("open");
};
