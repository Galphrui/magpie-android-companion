(() => {
  "use strict";
  document.documentElement.classList.add("magpie-android");

  const textInputs = new Set(["text", "search", "url", "tel", "email", "password", "number"]);
  const editRegion = "[role='dialog'], dialog, .modal, .editor, .pop, form";
  let tappedEditor = null;
  let tapTime = 0;

  function isEditor(node) {
    if (node instanceof HTMLInputElement) return !node.readOnly && textInputs.has(node.type);
    if (node instanceof HTMLTextAreaElement) return !node.readOnly;
    return node instanceof HTMLElement && node.isContentEditable;
  }

  function recordTap(event) {
    if (!event.isTrusted) return;
    const target = event.target instanceof Element ? event.target : null;
    const editor = target?.closest("input, textarea, [contenteditable]") ?? target?.closest("label")?.control;
    tappedEditor = isEditor(editor) ? editor : null;
    tapTime = performance.now();
  }

  function acceptsFocus(node) {
    if (!isEditor(node) || node === document.activeElement) return true;
    if (node === tappedEditor && performance.now() - tapTime < 500) return true;
    const active = document.activeElement;
    const region = isEditor(active) ? active.closest(editRegion) : null;
    return region !== null && node.closest(editRegion) === region;
  }

  document.addEventListener("pointerdown", recordTap, true);
  document.addEventListener("click", recordTap, true);
  const focus = HTMLElement.prototype.focus;
  HTMLElement.prototype.focus = function (...args) {
    // Page-driven opening of a form should not summon the Android keyboard.
    // Tapping a field or moving to it with Tab uses the browser's native focus.
    if (acceptsFocus(this)) return Reflect.apply(focus, this, args);
  };
  for (const type of [HTMLInputElement, HTMLTextAreaElement]) {
    const select = type.prototype.select;
    type.prototype.select = function () {
      if (acceptsFocus(this)) return Reflect.apply(select, this, []);
    };
  }

  document.addEventListener("mousemove", (event) => {
    // The upstream hover handler rebuilds the row. Chrome's touch-generated
    // mousemove would remove that row before the tap reaches its click handler.
    if (event.sourceCapabilities?.firesTouchEvents
        && event.target instanceof Element
        && event.target.closest("#pop #list li[data-i]")) event.stopPropagation();
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    let width = innerWidth;
    let heightOnly = false;
    window.addEventListener("resize", () => {
      heightOnly = innerWidth === width;
      width = innerWidth;
    }, true);

    const placement = new WeakMap();
    for (const [name, selector] of [
      ["closeAgentModels", ".am-pop"],
      ["closeProtoMenu", ".proto-menu"],
    ]) {
      const close = window[name];
      if (typeof close !== "function") continue;
      // These two upstream menus close on every resize, including the
      // keyboard resizing the same viewport while their own filter is in use.
      window[name] = function (event) {
        const active = document.activeElement;
        const menu = event?.type === "resize" && event.currentTarget === window && heightOnly
            && isEditor(active) && active.closest(selector);
        if (!menu) return Reflect.apply(close, this, arguments);

        let original = placement.get(menu);
        if (!original) {
          const { top, bottom, maxHeight } = menu.style;
          original = { top, bottom, maxHeight };
          placement.set(menu, original);
        }
        Object.assign(menu.style, original);
        const bounds = menu.getBoundingClientRect();
        if (bounds.top >= 8 && bounds.bottom <= innerHeight - 8) return;
        const space = Math.max(0, innerHeight - 16);
        menu.style.bottom = "auto";
        menu.style.top = Math.max(8, Math.min(bounds.top, innerHeight - 8 - Math.min(bounds.height, space))) + "px";
        menu.style.maxHeight = space + "px";
      };
    }
  }, { once: true });
})();
